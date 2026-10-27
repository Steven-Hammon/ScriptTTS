"""Visible first-run setup; hand off to pythonw once ready. Standard library only."""
from __future__ import annotations

import ctypes
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import venv
import hashlib
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from script_tts.config import DATA, FAST_ENV, FAST_PYTHON, KOKORO_ASSETS, LICENSE_URL, MODEL_BYTES, MODEL_ID, MODEL_PATH, MODEL_REVISION, MODELS, ROOT, Settings

ENV = ROOT / ".venv"
PYTHON = ENV / "Scripts" / "python.exe"
MANIFEST = DATA / "model-manifest.json"
CONSENT = DATA / "consent.json"
CREATE_NO_WINDOW = 0x08000000
SPEC = MODELS["qwen-large"]


def select_model(key):
    global SPEC, MODEL_ID, MODEL_PATH, MODEL_REVISION, MODEL_BYTES, MANIFEST, CONSENT
    SPEC = MODELS[key]
    MODEL_ID, MODEL_PATH, MODEL_REVISION, MODEL_BYTES = SPEC.repo, SPEC.path, SPEC.revision, SPEC.size
    MANIFEST, CONSENT = SPEC.manifest, SPEC.consent


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, indent=2), encoding="utf-8")
    temp.replace(path)


def model_ready() -> bool:
    try:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        if manifest["revision"] != MODEL_REVISION or not manifest["files"]:
            return False
        for name, size in manifest["files"].items():
            path = MODEL_PATH / name
            if not path.is_file() or path.stat().st_size != size:
                return False
        required = KOKORO_ASSETS if SPEC.backend == "kokoro" else ("model.safetensors", "speech_tokenizer/model.safetensors")
        return all(name in manifest["files"] for name in required)
    except (OSError, ValueError, KeyError, TypeError):
        return False


def runtime_ready() -> bool:
    if not PYTHON.is_file():
        return False
    packages = ['comtypes', 'win32com', 'sounddevice', 'soundfile', 'numpy', 'tkinter', 'num2words', 'httpx']
    if SPEC.backend == 'qwen-fast':
        return _has_modules(PYTHON, packages) and _has_modules(FAST_PYTHON, ['faster_qwen3_tts', 'torch', 'qwen_tts'])
    packages += ['kokoro_onnx'] if SPEC.backend == 'kokoro' else ['qwen_tts', 'torch', 'torchaudio', 'transformers', 'accelerate']
    result = subprocess.run([str(PYTHON), "-c", f"import importlib.util; names={packages!r}; raise SystemExit(not all(importlib.util.find_spec(n) is not None for n in names))"], creationflags=CREATE_NO_WINDOW)
    return result.returncode == 0


def _has_modules(python, packages):
    if not python.is_file():
        return False
    result = subprocess.run([str(python), "-c", f"import importlib.util; raise SystemExit(not all(importlib.util.find_spec(n) is not None for n in {packages!r}))"], creationflags=CREATE_NO_WINDOW)
    return result.returncode == 0


def has_consent() -> bool:
    try:
        value = json.loads(CONSENT.read_text(encoding="utf-8"))
        # The license acceptance belongs to the model family. The recorded
        # revision remains an audit of the original agreement; changing a
        # precision/export variant does not fabricate a new acceptance.
        return value.get("accepted") is True and value.get("model") == MODEL_ID and value.get("license") == "Apache-2.0"
    except (OSError, ValueError, TypeError):
        return False


def ask_consent(read=input, output=print) -> bool:
    output("\nScriptTTS first-run setup\n")
    output(f"The program wants to download {MODEL_ID}")
    output(f"({MODEL_BYTES / 1e9:.2f} GB, including {'preset voices' if SPEC.backend == 'kokoro' else 'its speech tokenizer'}).")
    if SPEC.backend == "kokoro":
        output("This includes the full-precision ONNX model and preset voices; Kokoro does not clone a WAV.")
        output("Runtime packages may add approximately 100–300 MB of downloads.\n")
    else:
        if SPEC.backend == "qwen-fast":
            output("This model uses preset voices and NVIDIA acceleration; no reference WAV is needed.")
        output("Python/PyTorch and other runtime packages may add approximately 3–6 GB of downloads.")
        output("Existing installed packages and cached downloads are reused.")
        output("Allow at least 16 GB of free disk space for installation and download caches.\n")
    output("Model license: Apache License 2.0")
    output(f"Read the model license: {LICENSE_URL}")
    output(f"Model card: https://huggingface.co/{MODEL_ID}")
    output("A copy of the license is included in MODEL-LICENSE.txt.\n")
    output("Do you agree to abide by the model license agreement and download?\n1 Agree\n2 Don't agree")
    while True:
        try:
            answer = read("Select 1 or 2: ").strip()
        except (EOFError, KeyboardInterrupt):
            return False
        if answer == "1":
            return True
        if answer == "2":
            return False
        output("Please enter 1 (Agree) or 2 (Don't agree).")


def install_runtime():
    if not PYTHON.exists():
        print("\nCreating a private Python environment…", flush=True)
        venv.EnvBuilder(with_pip=True).create(ENV)
    subprocess.run([str(PYTHON), "-m", "ensurepip", "--upgrade"], check=True)
    if SPEC.backend == "qwen-fast":
        if not _has_modules(PYTHON, ['comtypes', 'win32com', 'sounddevice', 'soundfile', 'numpy', 'tkinter', 'num2words', 'httpx']):
            subprocess.run([str(PYTHON), "-m", "pip", "install", "--cache-dir", str(ROOT / ".cache/pip"), "-r", str(ROOT / "requirements-ui.txt")], check=True)
        try:
            ctypes.WinDLL("nvcuda.dll")
        except OSError as exc:
            raise RuntimeError("Fast Qwen preset voices require an NVIDIA CUDA GPU. Choose Kokoro for CPU reading.") from exc
        if not FAST_PYTHON.exists():
            venv.EnvBuilder(with_pip=True).create(FAST_ENV)
        subprocess.run([str(FAST_PYTHON), "-m", "pip", "install", "--cache-dir", str(ROOT / ".cache/pip"), "torch==2.8.0", "torchaudio==2.8.0", "--index-url", "https://download.pytorch.org/whl/cu128"], check=True)
        subprocess.run([str(FAST_PYTHON), "-m", "pip", "install", "--cache-dir", str(ROOT / ".cache/pip"), "-r", str(ROOT / "requirements-fast.txt")], check=True)
        return
    if SPEC.backend == "kokoro":
        subprocess.run([str(PYTHON), "-m", "pip", "install", "--cache-dir", str(ROOT / ".cache" / "pip"), "-r", str(ROOT / "requirements-kokoro.txt")], check=True)
        return
    try:
        ctypes.WinDLL("nvcuda.dll")
        index = "https://download.pytorch.org/whl/cu128"
        print("Installing NVIDIA CUDA PyTorch…", flush=True)
    except OSError:
        index = "https://download.pytorch.org/whl/cpu"
        print("Installing CPU PyTorch…", flush=True)
    subprocess.run([str(PYTHON), "-m", "pip", "install", "--cache-dir", str(ROOT / ".cache" / "pip"), "torch==2.8.0", "torchaudio==2.8.0", "--index-url", index], check=True)
    subprocess.run([str(PYTHON), "-m", "pip", "install", "--cache-dir", str(ROOT / ".cache" / "pip"), "-r", str(ROOT / "requirements.txt")], check=True)


def download_model():
    # Executed inside the private environment. Metadata gives expected sizes;
    # the completion manifest is written only after every file is complete.
    if SPEC.backend == "kokoro":
        download_kokoro()
        return
    from huggingface_hub import HfApi, snapshot_download

    info = HfApi().model_info(MODEL_ID, revision=MODEL_REVISION, files_metadata=True)
    files = {item.rfilename: item.size for item in info.siblings if item.size is not None and item.rfilename != ".gitattributes"}
    print(f"\nDownloading {SPEC.label} ({sum(files.values()) / 1e9:.2f} GB). Interrupted downloads can be resumed.\n", flush=True)
    snapshot_download(repo_id=MODEL_ID, revision=MODEL_REVISION, local_dir=str(MODEL_PATH), allow_patterns=list(files), max_workers=4)
    for name, size in files.items():
        if (MODEL_PATH / name).stat().st_size != size:
            raise RuntimeError(f"Incomplete download: {name}. Run Start Reader.cmd again to resume.")
    write_json(MANIFEST, {"revision": MODEL_REVISION, "files": files})


def download_kokoro():
    MODEL_PATH.mkdir(parents=True, exist_ok=True)
    for name, (size, digest) in KOKORO_ASSETS.items():
        path = MODEL_PATH / name
        if path.exists() and path.stat().st_size == size:
            with path.open("rb") as source:
                if hashlib.file_digest(source, "sha256").hexdigest() == digest:
                    continue
        partial = path.with_suffix(path.suffix + ".part")
        offset = partial.stat().st_size if partial.exists() else 0
        if offset >= size:
            offset = 0
        headers = {"User-Agent": "ScriptTTS/1.0"}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        url = f"https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1/{name}"
        print(f"Downloading {name} ({size / 1e6:.1f} MB)…", flush=True)
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as response:
            if response.status != 206:
                offset = 0
            with partial.open("ab" if offset else "wb") as output:
                bucket = offset // 10_000_000
                while block := response.read(1024 * 1024):
                    output.write(block)
                    offset += len(block)
                    if offset // 10_000_000 > bucket:
                        bucket = offset // 10_000_000
                        print(f"  {min(100, offset * 100 // size)}%", flush=True)
        with partial.open("rb") as source:
            valid = hashlib.file_digest(source, "sha256").hexdigest() == digest
        if partial.stat().st_size != size or not valid:
            raise RuntimeError(f"Checksum verification failed for {name}. Run setup again to retry.")
        partial.replace(path)
    write_json(MANIFEST, {"revision": MODEL_REVISION, "files": {name: item[0] for name, item in KOKORO_ASSETS.items()}})


def main() -> int:
    if sys.platform != "win32":
        print("ScriptTTS currently requires Windows.")
        return 1
    prepare = "--prepare" in sys.argv
    if prepare:
        select_model(sys.argv[sys.argv.index("--prepare") + 1])
    elif "--model" in sys.argv:
        select_model(sys.argv[sys.argv.index("--model") + 1])
    elif "--download-model" not in sys.argv:
        select_model(Settings.load().model)
    if "--download-model" in sys.argv:
        if not has_consent():
            raise RuntimeError("Run Start Reader.cmd or --prepare first to review and accept this model's license.")
        download_model()
        return 0
    ready = model_ready() and runtime_ready()
    if not has_consent():
        if not ask_consent():
            print("\nDownload declined. Nothing will be installed or downloaded.")
            return 0
        write_json(CONSENT, {"accepted": True, "model": MODEL_ID, "revision": MODEL_REVISION, "license": "Apache-2.0", "accepted_at": datetime.now(timezone.utc).isoformat()})
    if not ready:
        minimum = (1 if SPEC.backend == "kokoro" else 16) * 1024**3
        if shutil.disk_usage(ROOT).free < minimum:
            raise RuntimeError(f"At least {minimum // 1024**3} GB of free space is required before setup.")
        if not runtime_ready():
            install_runtime()
        if not model_ready():
            download_python = FAST_PYTHON if SPEC.backend == "qwen-fast" else PYTHON
            subprocess.run([str(download_python), str(ROOT / "bootstrap.py"), "--download-model", "--model", SPEC.key], check=True, cwd=ROOT)
    if prepare:
        print(f"\n{SPEC.label} is ready. This window will close.", flush=True)
        return 0
    # A GUI child has no console. Returning closes a double-clicked CMD window;
    # launching from an existing terminal leaves that user's terminal alone.
    child_env = os.environ.copy()
    child_env["HF_HUB_DISABLE_TELEMETRY"] = "1"
    print("\nStarting ScriptTTS…", flush=True)
    subprocess.Popen([str(ENV / "Scripts" / "pythonw.exe"), "-m", "script_tts"], cwd=ROOT, env=child_env, creationflags=CREATE_NO_WINDOW)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (Exception, KeyboardInterrupt) as exc:
        print(f"\nSetup stopped: {exc}\nRun Start Reader.cmd again to retry.", flush=True)
        if "--prepare" in sys.argv:
            try:
                input("Press Enter to close this setup window.")
            except (EOFError, KeyboardInterrupt):
                pass
        raise SystemExit(1)
