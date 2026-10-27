from __future__ import annotations

import base64
import ctypes
import io
import json
import logging
import os
import queue
import re
import subprocess
import threading
import time
import uuid

from .config import DATA, FAST_PYTHON, QWEN_PRESETS, ROOT

log = logging.getLogger(__name__)


def qwen_speaker(voice):
    if voice.qwen_speaker != "Auto from description":
        match = next((item for item in QWEN_PRESETS[1:] if item.lower() == voice.qwen_speaker.lower()), None)
        if match is None:
            raise ValueError("Choose a supported Qwen preset voice.")
        return match.lower()
    text = voice.description.lower()
    named = next((name for name in QWEN_PRESETS[1:] if re.search(r"\b" + re.escape(name.lower()) + r"\b", text)), None)
    if named:
        return named.lower()
    male = bool(re.search(r"\b(man|male|boy|gentleman)\b", text))
    if voice.language == "Japanese":
        return "ono_anna"
    if voice.language == "Korean":
        return "sohee"
    return "aiden" if male else "vivian"


class FastQwenEngine:
    def __init__(self, status):
        self.status = status
        self.process = None
        self.job = None
        self.log_file = None
        self.messages = queue.Queue()

    def _start(self):
        if self.process is not None and self.process.poll() is None:
            return
        self.unload()
        if not FAST_PYTHON.is_file():
            raise RuntimeError("Click Prepare model to install the fast Qwen runtime.")
        DATA.mkdir(exist_ok=True)
        self.log_file = (DATA / "fast-runtime.log").open("a", encoding="utf-8")
        environment = os.environ.copy()
        environment.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1", PYTHONIOENCODING="utf-8")
        self.process = subprocess.Popen(
            [str(FAST_PYTHON), "-m", "script_tts.fast_worker"], cwd=ROOT,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log_file,
            text=True, encoding="utf-8", bufsize=1, env=environment, creationflags=0x08000000 | 0x00000004,
        )
        try:
            # The worker must die when the GUI exits, including os._exit().
            import win32api
            import win32job
            self.job = win32job.CreateJobObject(None, "ScriptTTS.QwenWorker." + uuid.uuid4().hex)
            limits = win32job.QueryInformationJobObject(self.job, win32job.JobObjectExtendedLimitInformation)
            limits["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            win32job.SetInformationJobObject(self.job, win32job.JobObjectExtendedLimitInformation, limits)
            handle = win32api.OpenProcess(0x0100 | 0x0001, False, self.process.pid)
            try:
                win32job.AssignProcessToJobObject(self.job, handle)
            finally:
                handle.Close()
            # Assign before execution: venv launchers may spawn a real Python
            # child immediately, which must inherit the same kill-on-close job.
            resume = ctypes.WinDLL("ntdll").NtResumeProcess
            resume.argtypes = [ctypes.c_void_p]
            resume.restype = ctypes.c_long
            if resume(int(self.process._handle)) != 0:
                raise RuntimeError("Could not resume the Qwen worker process.")
        except Exception:
            self.unload()
            raise
        self.messages = queue.Queue()
        process, messages = self.process, self.messages
        def read_output():
            try:
                for line in process.stdout:
                    try:
                        messages.put(json.loads(line))
                    except ValueError:
                        log.debug("Ignoring non-protocol output from Qwen worker")
            finally:
                messages.put({"type": "exit"})
        threading.Thread(target=read_output, name="qwen-worker-output", daemon=True).start()

    def _send(self, message):
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def synthesize(self, text, voice, cancel):
        from .engine import Cancelled
        if cancel.is_set():
            raise Cancelled()
        speaker = qwen_speaker(voice)
        self._start()
        request_id = uuid.uuid4().hex
        self._send({"type": "generate", "id": request_id, "text": text, "language": voice.language, "speaker": speaker})
        deadline = time.monotonic() + 180
        while True:
            if time.monotonic() > deadline:
                self.unload()
                raise RuntimeError(f"Qwen took too long to respond. See {DATA / 'fast-runtime.log'} and try Prepare model again.")
            if cancel.is_set():
                try:
                    self._send({"type": "cancel", "id": request_id})
                except (OSError, ValueError):
                    self.unload()
                raise Cancelled()
            try:
                response = self.messages.get(timeout=0.03)
            except queue.Empty:
                continue
            if response.get("type") == "exit":
                self.unload()
                raise RuntimeError(f"The optimized Qwen runtime stopped. See {DATA / 'fast-runtime.log'}")
            if response.get("id") != request_id:
                continue  # discard responses belonging to a stopped reading
            kind = response["type"]
            if kind == "status":
                self.status(response["text"])
            elif kind == "error":
                self.unload()
                raise RuntimeError(response["text"])
            elif kind == "cancelled":
                raise Cancelled()
            elif kind == "audio":
                import numpy as np
                audio = np.load(io.BytesIO(base64.b64decode(response["data"])), allow_pickle=False)
                if cancel.is_set():
                    raise Cancelled()
                return audio, response["rate"]

    def unload(self):
        process = self.process
        if process is not None:
            try:
                if process.poll() is None:
                    self._send({"type": "close"})
                    process.wait(timeout=2)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=3)
            finally:
                for stream in (process.stdin, process.stdout):
                    if stream:
                        stream.close()
                self.process = None
        if self.job is not None:
            self.job.Close()
            self.job = None
        if self.log_file is not None:
            self.log_file.close()
            self.log_file = None
