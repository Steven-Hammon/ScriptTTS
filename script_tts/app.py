from __future__ import annotations

import queue
import os
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, ttk

from .config import DATA, MODELS, QWEN_PRESETS, ROOT, Settings, model_ready
from .engine import KOKORO_PRESETS
from .hotkey import GlobalHotkey, MODIFIER_KEYS, label, pressed_modifiers
from .reader import Reader
from .textprep import OllamaClient, choose_default_model

LANGUAGES = ("English", "Auto", "Chinese", "Japanese", "Korean", "German", "French", "Russian", "Portuguese", "Spanish", "Italian")


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("ScriptTTS · Reader")
        self.root.geometry("740x820")
        self.root.minsize(700, 780)
        self.root.configure(bg="#f3f5f8")
        self.settings = Settings.load()
        self.events = queue.Queue()
        self.recording = False
        self.save_timer = None
        self.closing = False
        self.setup_process = None
        self._loading = False
        self._blink_timer = None
        self._blink_red = False
        self._models_warming = False
        self.voice = self.settings.voice()
        self._build()
        self.reader = Reader(lambda kind, value: self.events.put((kind, value)))
        self.hotkey = GlobalHotkey(lambda hwnd: self.events.put(("hotkey", hwnd)))
        ok, error = self.hotkey.register(self.settings.modifiers, self.settings.key)
        if not ok:
            self.status.set(error)
        elif not model_ready(self.settings.model):
            self.status.set("Click Prepare model to install the selected model.")
        self._refresh_backend()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(30, self._poll)
        self.root.after(100, lambda: self._refresh_ollama(silent=True))

    def _build(self):
        from .ui import build
        build(self)

    def _browse(self):
        path = filedialog.askopenfilename(title="Choose a voice sample", filetypes=[("WAV audio", "*.wav")], initialdir=str(Path(self.wav.get()).parent) if self.wav.get() else None)
        if path:
            self.wav.set(path)
            transcript = Path(path).with_suffix(".txt")
            if transcript.is_file():
                try:
                    self.transcript.set(transcript.read_text(encoding="utf-8-sig").strip())
                except (OSError, UnicodeError):
                    self.status.set("WAV selected. Its matching TXT transcript could not be read as UTF-8.")
        self.wav_entry.focus_set()

    def _settings_changed(self, *_):
        self.settings.wav = self.wav.get().strip().strip('"')
        self.settings.transcript = self.transcript.get().strip()
        self.settings.language = self.language.get()
        self.settings.description = self.description.get().strip()
        self.settings.speed = round(min(2.0, max(0.5, self.speed.get())), 2)
        self.settings.preset = self.preset.get()
        self.settings.qwen_speaker = self.qwen_speaker.get()
        self.settings.normalize_text = self.normalize.get()
        self.settings.ollama_enabled = self.use_ollama.get()
        self.settings.ollama_model = self.ollama_model.get()
        self.settings.cues_enabled = self.cues.get()
        self.speed_text.set(f"{self.settings.speed:.2f}×")
        new_voice = self.settings.voice()
        if new_voice != self.voice and self._models_warming:
            self.reader.stop(announce=False)
            self._models_warming = False
            self.load_button.configure(state="normal")
            self._set_status("Settings changed · click Load Models again")
        self.voice = new_voice
        if self.save_timer:
            self.root.after_cancel(self.save_timer)
        self.save_timer = self.root.after(300, self._save)

    def _model_changed(self, _event=None):
        self.reader.stop(announce=False)
        self.settings.model = next(key for key, spec in MODELS.items() if spec.label == self.model_label.get())
        self._settings_changed()
        self._refresh_backend()
        self.status.set("Ready · press the hotkey in your document" if model_ready(self.settings.model) else "Click Prepare model to download this model after reviewing its license.")

    def _set_status(self, text, mode="normal"):
        self.status.set(text)
        self._loading = mode == "loading"
        if self._blink_timer is not None:
            self.root.after_cancel(self._blink_timer)
            self._blink_timer = None
        if self._loading:
            self._blink_red = False
            self._blink_status()
        else:
            self.status_label.configure(style="Ready.TLabel" if mode == "ready" else "Loading.TLabel" if mode == "error" else "Status.TLabel")

    def _blink_status(self):
        if self.closing or not self._loading:
            return
        self._blink_red = not self._blink_red
        self.status_label.configure(style="Loading.TLabel" if self._blink_red else "Status.TLabel")
        self._blink_timer = self.root.after(450, self._blink_status)

    def _refresh_ollama(self, silent=False):
        self.refresh_button.configure(state="disabled")
        def refresh():
            try:
                self.events.put(("ollama_models", OllamaClient().models()))
            except Exception as exc:
                self.events.put(("ollama_error", (str(exc), silent)))
        threading.Thread(target=refresh, name="ollama-model-list", daemon=True).start()

    def _load_models(self):
        self._settings_changed()
        self._save()
        self.load_button.configure(state="disabled")
        self._models_warming = True
        self._set_status("Loading selected speech and Ollama models…", "loading")
        self.reader.load_models(self.voice)

    def _refresh_backend(self):
        kokoro = self.settings.model == "kokoro"
        fast = MODELS[self.settings.model].backend == "qwen-fast"
        self.description_entry.configure(state="normal" if kokoro or fast else "disabled")
        self.speed_slider.configure(state="normal" if kokoro else "disabled")
        self.preset_box.configure(textvariable=self.qwen_speaker if fast else self.preset, values=QWEN_PRESETS if fast else KOKORO_PRESETS, state="readonly" if kokoro or fast else "disabled")
        for widget in (self.wav_entry, self.browse_button, self.transcript_entry):
            widget.configure(state="disabled" if kokoro or fast else "normal")
        if fast:
            hint = "Qwen presets · NVIDIA acceleration. Description picks a preset; 0.6B does not design a new voice. Speed applies to Kokoro."
        elif kokoro:
            hint = "Kokoro matches gender/accent to a preset; other traits depend on that preset. Speed: 0.5×–2.0×."
        else:
            hint = "Qwen clones the WAV below. Description and speed apply to preset voices / Kokoro."
        self.backend_hint.set(hint)
        installed = model_ready(self.settings.model)
        self.prepare_button.configure(text="Installed" if installed else "Prepare model", state="disabled" if installed or self.setup_process else "normal")

    def _prepare_model(self):
        if self.setup_process:
            return
        self.reader.stop()
        self._save()
        console_python = str(Path(sys.executable).with_name("python.exe"))
        setup_env = os.environ.copy()
        # Inference deliberately runs offline; a separately approved setup
        # process must still be able to download another selected model.
        setup_env.pop("HF_HUB_OFFLINE", None)
        setup_env.pop("TRANSFORMERS_OFFLINE", None)
        self.setup_process = subprocess.Popen([console_python, str(ROOT / "bootstrap.py"), "--prepare", self.settings.model], cwd=ROOT, env=setup_env, creationflags=0x00000010)
        self.status.set("Setup is open in CMD · review the license and choose 1 or 2 there")
        self._refresh_backend()

    def _check_setup(self):
        if self.setup_process and self.setup_process.poll() is not None:
            code = self.setup_process.returncode
            self.setup_process = None
            self._refresh_backend()
            self.status.set("Selected model is ready" if model_ready(self.settings.model) else "Setup was declined or did not finish. Click Prepare model to retry.")

    def _save(self):
        self.save_timer = None
        try:
            self.settings.save()
        except OSError as exc:
            self.status.set(f"Could not save settings: {exc}")

    def _record_start(self, _event=None):
        if not hasattr(self, "hotkey") or self.recording:
            return "break"
        self.reader.stop(announce=False)
        self.hotkey.pause()
        self.recording = True
        self.hotkey_entry.focus_set()
        return "break"

    def _release_hotkey_focus(self):
        if self.root.focus_get() == self.hotkey_entry:
            self.root.focus_set()

    def _hotkey_focus_in(self, _event=None):
        # Notebook focus restoration and keyboard navigation cannot arm capture.
        if not self.recording:
            self.root.after_idle(self._release_hotkey_focus)

    def _hotkey_outside_click(self, event):
        if event.widget != self.hotkey_entry:
            self._record_leave()
            self._release_hotkey_focus()

    def _hotkey_tab_changed(self, _event=None):
        self._record_leave()
        self._release_hotkey_focus()

    def _record_key(self, event):
        if not self.recording:
            return "break"
        if event.keycode in MODIFIER_KEYS:
            return "break"
        if event.keycode in {9, 27}:
            self._record_leave()
            self.root.focus_set()
            return "break"
        modifiers = pressed_modifiers()
        if not modifiers & 3:
            self.status.set("Shortcut unchanged · include Ctrl or Alt with the key")
            return "break"
        self.hotkey.suppress_until_release(modifiers, event.keycode)
        ok, error = self.hotkey.register(modifiers, event.keycode)
        if ok:
            self.settings.modifiers = modifiers
            self.settings.key = event.keycode
            self.settings.hotkey_label = label(modifiers, event.keycode).upper()
            self.hotkey_text.set(self.settings.hotkey_label)
            self.recording = False
            self._save()
            self.status.set("Hotkey saved · click in your document to read")
            self.root.focus_set()
        else:
            self.status.set(error)
        return "break"

    def _record_leave(self, _event=None):
        if self.recording:
            self.recording = False
            self.hotkey_text.set(self.settings.hotkey_label.upper())
            ok, error = self.hotkey.register(self.settings.modifiers, self.settings.key)
            if not ok:
                self.status.set(error)

    def _poll(self):
        if self.closing:
            return
        self._check_setup()
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "hotkey":
                    self.reader.toggle(value, self.voice)
                elif kind == "loading":
                    self._set_status(value, "loading")
                elif kind == "ready":
                    self._set_status(value, "ready")
                elif kind == "error":
                    self._set_status(value, "error")
                elif kind == "tracking_warning":
                    self._set_status(value, "error")
                elif kind == "status":
                    self._set_status(value)
                elif kind == "model":
                    if str(value).startswith(("Loading", "Preparing")):
                        self._set_status(value, "loading")
                    elif self._loading:
                        self.status.set("Loading models · " + str(value))
                elif kind == "busy":
                    self._models_warming = bool(value)
                    self.load_button.configure(state="disabled" if value else "normal")
                elif kind == "ollama_models":
                    self.refresh_button.configure(state="normal")
                    self.ollama_box.configure(values=value)
                    if not self.ollama_model.get() and value:
                        self.ollama_model.set(choose_default_model(value))
                elif kind == "ollama_error":
                    self.refresh_button.configure(state="normal")
                    message, silent = value
                    if not silent:
                        self._set_status(message, "error")
                elif kind == "sentence":
                    self.current.set(value[:280] + ("…" if len(value) > 280 else ""))
                elif kind == "spoken":
                    self.spoken.set(value[:280] + ("…" if len(value) > 280 else ""))
                elif kind == "active":
                    self.stop_button.configure(state="normal" if value else "disabled")
        except queue.Empty:
            pass
        self.root.after(30, self._poll)

    def close(self):
        self.closing = True
        self._save()
        self.hotkey.close()
        self.reader.close()
        self.root.destroy()
