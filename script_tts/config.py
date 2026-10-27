from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
MODEL_ID = "Qwen/Qwen3-TTS-12Hz-1.7B-Base"
MODEL_REVISION = "fd4b254389122332181a7c3db7f27e918eec64e3"
MODEL_PATH = ROOT / "models" / "Qwen3-TTS-12Hz-1.7B-Base"
LICENSE_URL = "https://www.apache.org/licenses/LICENSE-2.0"
MODEL_BYTES = 4_544_229_700
FAST_ENV = ROOT / ".fast-venv"
FAST_PYTHON = FAST_ENV / "Scripts" / "python.exe"
QWEN_PRESETS = ("Auto from description", "Vivian", "Serena", "Ryan", "Aiden", "Dylan", "Eric", "Uncle_Fu", "Ono_Anna", "Sohee")


@dataclass(frozen=True)
class ModelSpec:
    key: str
    label: str
    backend: str
    repo: str
    revision: str
    size: int
    folder: str

    @property
    def path(self) -> Path:
        return ROOT / "models" / self.folder

    @property
    def manifest(self) -> Path:
        return DATA / ("model-manifest.json" if self.key == "qwen-large" else f"{self.key}-manifest.json")

    @property
    def consent(self) -> Path:
        return DATA / ("consent.json" if self.key == "qwen-large" else f"{self.key}-consent.json")


MODELS = {
    "qwen-preset-fast": ModelSpec("qwen-preset-fast", "Qwen3 0.6B · fast preset voices (NVIDIA)", "qwen-fast", "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice", "85e237c12c027371202489a0ec509ded67b5e4b5", 2_498_388_392, "Qwen3-TTS-12Hz-0.6B-CustomVoice"),
    "qwen-large": ModelSpec("qwen-large", "Qwen3 1.7B · voice clone", "qwen", MODEL_ID, MODEL_REVISION, MODEL_BYTES, MODEL_PATH.name),
    "qwen-small": ModelSpec("qwen-small", "Qwen3 0.6B · faster voice clone", "qwen", "Qwen/Qwen3-TTS-12Hz-0.6B-Base", "5d83992436eae1d760afd27aff78a71d676296fc", 2_516_106_051, "Qwen3-TTS-12Hz-0.6B-Base"),
    "kokoro": ModelSpec("kokoro", "Kokoro 82M · fastest, preset voices", "kokoro", "hexgrad/Kokoro-82M", "onnx-model-files-v1.1-fp32", 353_719_767, "Kokoro-82M"),
}
KOKORO_ASSETS = {
    "kokoro-v1.0.onnx": (325_505_369, "beb0d1848dee9a49da392cc3df26958d46cfa35d321edf434f52949153f0df3a"),
    "voices-v1.0.bin": (28_214_398, "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d"),
}


def model_ready(key: str) -> bool:
    spec = MODELS[key]
    if spec.backend == "qwen-fast" and (not FAST_PYTHON.is_file() or not (FAST_ENV / "Lib/site-packages/faster_qwen3_tts/__init__.py").is_file()):
        return False
    try:
        manifest = json.loads(spec.manifest.read_text(encoding="utf-8"))
        if manifest["revision"] != spec.revision or not manifest["files"]:
            return False
        required = KOKORO_ASSETS if spec.backend == "kokoro" else ("model.safetensors", "speech_tokenizer/model.safetensors")
        return all(name in manifest["files"] for name in required) and all((spec.path / name).is_file() and (spec.path / name).stat().st_size == size for name, size in manifest["files"].items())
    except (OSError, ValueError, TypeError, KeyError):
        return False


@dataclass(frozen=True)
class Voice:
    wav: str = ""
    transcript: str = ""
    language: str = "English"
    model: str = "qwen-large"
    description: str = "20 year old woman"
    speed: float = 1.0
    preset: str = "Auto from description"
    qwen_speaker: str = "Auto from description"
    normalize_text: bool = True
    ollama_enabled: bool = False
    ollama_model: str = ""
    cues_enabled: bool = True


@dataclass
class Settings:
    modifiers: int = 0x0002 | 0x0001  # Ctrl + Alt
    key: int = 0xBF  # physical / key, even with Shift held
    hotkey_label: str = "CTRL+ALT+/"
    wav: str = ""
    transcript: str = ""
    language: str = "English"
    model: str = "qwen-large"
    description: str = "20 year old woman"
    speed: float = 1.0
    preset: str = "Auto from description"
    qwen_speaker: str = "Auto from description"
    normalize_text: bool = True
    ollama_enabled: bool = False
    ollama_model: str = ""
    cues_enabled: bool = True

    @classmethod
    def load(cls, path: Path = DATA / "settings.json") -> Settings:
        try:
            values = json.loads(path.read_text(encoding="utf-8"))
            result = cls(**{k: v for k, v in values.items() if k in cls.__dataclass_fields__})
            if not isinstance(result.modifiers, int) or not isinstance(result.key, int):
                return cls()
            if not 1 <= result.key <= 255 or not result.modifiers & 0x000B:
                return cls()
            if not all(isinstance(getattr(result, k), str) for k in ("wav", "transcript", "language", "hotkey_label", "description", "model", "preset", "qwen_speaker")):
                return cls()
            if result.model not in MODELS:
                result.model = "qwen-large"
            if not isinstance(result.ollama_model, str):
                result.ollama_model = ""
            for key in ("normalize_text", "ollama_enabled", "cues_enabled"):
                if not isinstance(getattr(result, key), bool):
                    setattr(result, key, getattr(cls(), key))
            if not isinstance(result.speed, (int, float)) or not 0.5 <= result.speed <= 2.0:
                result.speed = 1.0
            return result
        except (OSError, ValueError, TypeError):
            return cls()

    def save(self, path: Path = DATA / "settings.json") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        temp.replace(path)

    def voice(self) -> Voice:
        return Voice(self.wav, self.transcript.strip(), self.language, self.model, self.description, self.speed, self.preset, self.qwen_speaker, self.normalize_text, self.ollama_enabled, self.ollama_model, self.cues_enabled)
