from __future__ import annotations

import logging
import os
import threading
import re
from pathlib import Path

from .config import MODELS, Voice, model_ready

log = logging.getLogger(__name__)


class Cancelled(Exception):
    pass


def validate_voice(voice: Voice) -> None:
    path = Path(voice.wav)
    if not voice.wav or not path.is_file() or path.suffix.lower() != ".wav":
        raise ValueError("Choose an existing .wav file to clone first.")
    import numpy as np
    import soundfile as sf

    info = sf.info(str(path))
    if not 1 <= info.duration <= 60:
        raise ValueError("Use a voice sample between 1 and 60 seconds (3–15 seconds is ideal).")
    audio, _ = sf.read(str(path), dtype="float32")
    if not np.isfinite(audio).all() or np.max(np.abs(audio), initial=0) < 0.0001:
        raise ValueError("The WAV sample is silent or contains invalid audio.")


class QwenEngine:
    """Only the single inference thread may call this object."""

    def __init__(self, status, model_key="qwen-large"):
        self.status = status
        self.model = None
        self.prompt = None
        self.prompt_key = None
        self.spec = MODELS[model_key]

    def load(self):
        if self.model is not None:
            return
        self.status(f"Loading {self.spec.label}…")
        # Model files and the bundled speech tokenizer are already downloaded by
        # bootstrap. Inference is local and never contacts Hugging Face.
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
        import torch
        from qwen_tts import Qwen3TTSModel

        cuda = torch.cuda.is_available()
        self.model = Qwen3TTSModel.from_pretrained(
            str(self.spec.path),
            device_map="cuda:0" if cuda else "cpu",
            dtype=torch.bfloat16 if cuda else torch.float32,
            attn_implementation="sdpa",
            local_files_only=True,
        )
        self.status("Ready · NVIDIA GPU" if cuda else "Ready · CPU (generation will be slower)")

    def synthesize(self, text: str, voice: Voice, cancel: threading.Event):
        if cancel.is_set():
            raise Cancelled()
        self.load()
        if cancel.is_set():
            raise Cancelled()
        path = Path(voice.wav)
        key = (str(path.resolve()), path.stat().st_mtime_ns, path.stat().st_size, voice.transcript)
        if key != self.prompt_key:
            validate_voice(voice)
            self.status("Preparing cloned voice…")
            self.prompt = self.model.create_voice_clone_prompt(
                ref_audio=str(path),
                ref_text=voice.transcript or None,
                x_vector_only_mode=not bool(voice.transcript),
            )
            self.prompt_key = key
        if cancel.is_set():
            raise Cancelled()
        # qwen-tts 0.1.1 drops unknown generation kwargs before calling its
        # talker, so passing stopping_criteria would silently do nothing.
        # A scoped public PyTorch hook aborts at the next talker forward pass.
        def check_cancel(_module, _args):
            if cancel.is_set():
                raise Cancelled()

        hook = self.model.model.talker.register_forward_pre_hook(check_cancel)
        try:
            wavs, rate = self.model.generate_voice_clone(
                text=text,
                language=voice.language,
                voice_clone_prompt=self.prompt,
                max_new_tokens=2048,
            )
        finally:
            hook.remove()
        if cancel.is_set():
            raise Cancelled()
        return wavs[0], rate

    def unload(self):
        self.model = self.prompt = self.prompt_key = None
        import gc
        gc.collect()
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()


# Descriptions select a real preset; Kokoro does not synthesize an arbitrary
# described voice or clone the reference WAV. Explicit preset names win.
KOKORO_PRESETS = ("Auto from description", "af_heart", "af_bella", "af_nicole", "af_sarah", "af_sky", "am_adam", "am_michael", "am_puck", "bf_emma", "bf_isabella", "bm_george", "bm_lewis")


def kokoro_voice(voice: Voice) -> tuple[str, str]:
    description = voice.description.lower()
    if voice.preset != "Auto from description":
        preset = voice.preset
    else:
        explicit = re.search(r"\b([abefhijpz][fm]_[a-z]+)\b", description)
        male = bool(re.search(r"\b(man|male|boy|gentleman)\b", description))
        british = bool(re.search(r"\b(british|english accent|uk|london)\b", description))
        if explicit:
            preset = explicit.group(1)
        elif voice.language == "French":
            preset = "ff_siwis"
        elif voice.language == "Spanish":
            preset = "em_alex" if male else "ef_dora"
        elif voice.language == "Italian":
            preset = "im_nicola" if male else "if_sara"
        elif voice.language == "Portuguese":
            preset = "pm_alex" if male else "pf_dora"
        elif voice.language in {"English", "Auto"}:
            preset = ("bm_george" if male else "bf_emma") if british else ("am_michael" if male else "af_heart")
        else:
            raise ValueError("Kokoro currently supports English, French, Spanish, Italian and Portuguese in this reader. Use Qwen for the other languages.")
    if not re.fullmatch(r"[abefhijpz][fm]_[a-z]+", preset):
        raise ValueError("Choose a valid Kokoro preset or Auto from description.")
    language = {"a": "en-us", "b": "en-gb", "e": "es", "f": "fr-fr", "i": "it", "p": "pt-br"}.get(preset[0])
    if language is None:
        raise ValueError("Choose an English, French, Spanish, Italian or Portuguese Kokoro preset.")
    return preset, language


class KokoroEngine:
    def __init__(self, status):
        self.status = status
        self.model = None

    def synthesize(self, text, voice, cancel):
        if cancel.is_set():
            raise Cancelled()
        if not 0.5 <= voice.speed <= 2.0:
            raise ValueError("Kokoro speed must be between 0.5× and 2.0×.")
        if self.model is None:
            self.status("Loading Kokoro 82M…")
            from kokoro_onnx import Kokoro
            import onnxruntime as ort
            directory = MODELS["kokoro"].path
            options = ort.SessionOptions()
            # Avoid the default all-core thread pool delaying short sentences.
            options.intra_op_num_threads = min(os.cpu_count() or 4, 4)
            session = ort.InferenceSession(str(directory / "kokoro-v1.0.onnx"), sess_options=options, providers=["CPUExecutionProvider"])
            self.model = Kokoro.from_session(session, str(directory / "voices-v1.0.bin"))
        if cancel.is_set():
            raise Cancelled()
        preset, language = kokoro_voice(voice)
        if preset not in self.model.voices:
            raise ValueError(f"Kokoro voice '{preset}' is not available. Choose another preset.")
        self.status(f"Kokoro · {preset} · {voice.speed:.2f}×")
        result = self.model.create(text, voice=preset, lang=language, speed=voice.speed)
        if cancel.is_set():
            raise Cancelled()
        return result

    def unload(self):
        self.model = None


class SpeechEngine:
    def __init__(self, status):
        self.status = status
        self.current_key = None
        self.backend = None

    def synthesize(self, text, voice, cancel):
        if cancel.is_set():
            raise Cancelled()
        if not model_ready(voice.model):
            raise ValueError("The selected model is not installed. Click Prepare model in ScriptTTS to review its license and download.")
        if self.current_key != voice.model:
            if self.backend is not None:
                self.backend.unload()
                self.backend = None
                self.current_key = None
            if MODELS[voice.model].backend == "qwen-fast":
                from .fast_engine import FastQwenEngine
                self.backend = FastQwenEngine(self.status)
            else:
                self.backend = KokoroEngine(self.status) if voice.model == "kokoro" else QwenEngine(self.status, voice.model)
            self.current_key = voice.model
        return self.backend.synthesize(text, voice, cancel)


from .audio import AudioPlayer
