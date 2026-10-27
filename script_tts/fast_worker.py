"""Persistent CUDA worker, run only by .fast-venv to isolate Transformers versions."""
from __future__ import annotations

import base64
import contextlib
import io
import json
import logging
import os
import queue
import sys
import threading

from .config import MODELS


def generate_audio(model, request, cancel):
    import numpy as np

    pieces, rate = [], 24000
    stream = model.generate_custom_voice_streaming(
        text=request["text"], speaker=request["speaker"], language=request["language"],
        max_new_tokens=2048, chunk_size=4,
    )
    try:
        for audio, rate, _timing in stream:
            if cancel.is_set():
                return None
            pieces.append(np.asarray(audio, dtype=np.float32).ravel())
    finally:
        stream.close()
    if cancel.is_set():
        return None
    if not pieces or not any(piece.size for piece in pieces):
        raise RuntimeError("Qwen did not generate audio. Try another sentence or preset.")
    audio = np.concatenate(pieces)
    if not np.isfinite(audio).all():
        raise RuntimeError("Qwen generated invalid audio. Try another sentence.")
    output = io.BytesIO()
    np.save(output, audio, allow_pickle=False)
    return {"type": "audio", "rate": int(rate), "data": base64.b64encode(output.getvalue()).decode("ascii")}


def serve(source, output, loader):
    commands = queue.Queue()
    events, guard = {}, threading.Lock()
    def send(message):
        output.write(json.dumps(message) + "\n")
        output.flush()

    def listen():
        try:
            for line in source:
                message = json.loads(line)
                action = message.get("type")
                if action in {"cancel", "close"}:
                    with guard:
                        for key, event in events.items():
                            if action == "close" or key == message.get("id"):
                                event.set()
                    if action == "close":
                        break
                elif action == "generate":
                    event = threading.Event()
                    with guard:
                        events[message["id"]] = event
                    commands.put((message, event))
        except Exception:
            logging.exception("Worker input failed")
        finally:
            with guard:
                for event in events.values():
                    event.set()
            commands.put(None)

    threading.Thread(target=listen, name="worker-input", daemon=True).start()
    model = None
    while command := commands.get():
        request, cancel = command
        request_id = request["id"]
        try:
            if cancel.is_set():
                send({"type": "cancelled", "id": request_id})
                continue
            with contextlib.redirect_stdout(sys.stderr):
                if model is None:
                    send({"type": "status", "id": request_id, "text": "Loading Qwen preset model and preparing CUDA graphs…"})
                    model = loader()
                result = None if cancel.is_set() else generate_audio(model, request, cancel)
            send(dict(result or {"type": "cancelled"}, id=request_id))
        except Exception as exc:
            logging.exception("Fast Qwen generation failed")
            send({"type": "error", "id": request_id, "text": str(exc)})
        finally:
            with guard:
                events.pop(request_id, None)


def load_model():
    import torch
    from faster_qwen3_tts import FasterQwen3TTS

    if not torch.cuda.is_available():
        raise RuntimeError("Fast Qwen presets require an NVIDIA CUDA GPU. Choose Kokoro or a standard Qwen clone model on CPU.")
    model = FasterQwen3TTS.from_pretrained(
        str(MODELS["qwen-preset-fast"].path), device="cuda", dtype=torch.bfloat16,
        attn_implementation="sdpa", backend="torch", local_files_only=True,
    )
    model.warmup(prefill_len=100)
    return model


def main():
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # Initialize native libraries before a background thread blocks on stdin.
    # Some Windows NumPy/OpenBLAS builds deadlock during DLL initialization if
    # another Python thread already holds the pipe reader's I/O lock.
    logging.info("Initializing numerical libraries before the input listener")
    with contextlib.redirect_stdout(sys.stderr):
        import numpy
        import torch
        import qwen_tts
        import faster_qwen3_tts
    logging.info("Numerical libraries ready")
    serve(sys.stdin, sys.stdout, load_model)


if __name__ == "__main__":
    main()
