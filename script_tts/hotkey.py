from __future__ import annotations

import ctypes
import queue
import threading
from ctypes import wintypes

from .win32 import foreground, user32

_SPECIAL = {0xBF: "/", 0xBE: ".", 0xBC: ",", 0xBA: ";", 0xDE: "'", 0xDB: "[", 0xDD: "]", 0xDC: "\\", 0xBD: "-", 0xBB: "=", 0xC0: "`", 0x20: "Space", 0x0D: "Enter", 0x1B: "Escape", 0x09: "Tab"}
MODIFIER_KEYS = {16, 17, 18, 91, 92, 160, 161, 162, 163, 164, 165}


def label(modifiers: int, key: int) -> str:
    parts = [name for flag, name in ((2, "Ctrl"), (4, "Shift"), (1, "Alt"), (8, "Win")) if modifiers & flag]
    name = _SPECIAL.get(key)
    if name is None:
        name = chr(key) if 48 <= key <= 90 else f"F{key - 111}" if 112 <= key <= 135 else f"Key {key}"
    return "+".join(parts + [name])


def pressed_modifiers() -> int:
    return sum(flag for vk, flag in ((0x11, 2), (0x10, 4), (0x12, 1), (0x5B, 8)) if user32.GetKeyState(vk) & 0x8000)


class GlobalHotkey:
    """RegisterHotKey consumes the chord, so it never inserts '/' into a document."""

    def __init__(self, callback):
        self.callback = callback
        self.commands: queue.Queue = queue.Queue()
        self.suppressed_chord = None
        self.thread = threading.Thread(target=self._run, name="global-hotkey", daemon=True)
        self.thread.start()

    def register(self, modifiers: int, key: int) -> tuple[bool, str]:
        reply: queue.Queue = queue.Queue()
        self.commands.put((modifiers, key, reply))
        try:
            return reply.get(timeout=3)
        except queue.Empty:
            return False, "The hotkey service did not respond."

    def close(self):
        self.commands.put(None)
        self.thread.join(timeout=2)

    def suppress_until_release(self, modifiers, key):
        self.suppressed_chord = (modifiers, key)

    def pause(self):
        reply: queue.Queue = queue.Queue()
        self.commands.put((None, None, reply))
        try:
            reply.get(timeout=3)
        except queue.Empty:
            pass

    def _run(self):
        user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
        current_id = None
        msg = wintypes.MSG()
        while True:
            try:
                command = self.commands.get(timeout=0.015)
                if command is None:
                    break
                mods, key, reply = command
                if mods is None:
                    if current_id is not None:
                        user32.UnregisterHotKey(None, current_id)
                    current_id = None
                    reply.put((True, ""))
                    continue
                next_id = 100 if current_id != 100 else 101
                # Register the replacement before releasing the working shortcut.
                if user32.RegisterHotKey(None, next_id, mods | 0x4000, key):
                    if current_id is not None:
                        user32.UnregisterHotKey(None, current_id)
                    current_id = next_id
                    reply.put((True, ""))
                else:
                    error = ctypes.get_last_error()
                    reply.put((False, f"{label(mods, key)} is unavailable (Windows error {error}). Choose another shortcut."))
            except queue.Empty:
                pass
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                if msg.message == 0x0312 and msg.wParam == current_id and self.suppressed_chord is None:
                    self.callback(foreground())
            if self.suppressed_chord is not None:
                mods, key = self.suppressed_chord
                keys = [key] + [vk for flag, vk in ((2, 0x11), (4, 0x10), (1, 0x12), (8, 0x5B)) if mods & flag]
                if not any(user32.GetAsyncKeyState(vk) & 0x8000 for vk in keys):
                    self.suppressed_chord = None
        if current_id is not None:
            user32.UnregisterHotKey(None, current_id)
