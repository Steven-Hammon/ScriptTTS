from __future__ import annotations

import ctypes
import logging
import os
import sys
import tkinter as tk
from tkinter import messagebox

from .config import DATA


def main():
    DATA.mkdir(exist_ok=True)
    logging.basicConfig(filename=DATA / "reader.log", level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s", encoding="utf-8")
    # A named mutex prevents two background readers fighting over the hotkey/GPU.
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.restype = ctypes.c_void_p
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    mutex = kernel.CreateMutexW(None, False, "Local\\ScriptTTS.Reader.1")
    if ctypes.get_last_error() == 183:
        messagebox.showinfo("ScriptTTS", "ScriptTTS is already running. Use its existing window.")
        return
    try:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
        from .app import App
        root = tk.Tk()
        root.report_callback_exception = lambda kind, value, trace: logging.getLogger(__name__).error("GUI callback failed", exc_info=(kind, value, trace))
        App(root)
        root.mainloop()
    except Exception as exc:
        logging.exception("Startup failed")
        messagebox.showerror("ScriptTTS could not start", f"{exc}\n\nSee {DATA / 'reader.log'}")
        raise
    finally:
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel.CloseHandle(mutex)
    # A CUDA call can outlive cancellation briefly. All audio and hotkeys are
    # already stopped; do not leave an invisible process holding GPU memory.
    logging.shutdown()
    os._exit(0)


if __name__ == "__main__":
    main()
