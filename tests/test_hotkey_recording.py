import tkinter as tk
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from script_tts.app import App
from script_tts.config import Settings


class HotkeyRecordingTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.hotkey = Mock()
        self.hotkey.register.return_value = (True, '')
        self.patches = [patch('script_tts.app.Settings.load', return_value=Settings()), patch.object(Settings, 'save'), patch('script_tts.app.Reader'), patch('script_tts.app.GlobalHotkey', return_value=self.hotkey), patch('script_tts.app.OllamaClient')]
        for item in self.patches:
            item.start()
        self.app = App(self.root)

    def tearDown(self):
        self.app.close()
        for item in reversed(self.patches):
            item.stop()

    def test_default_and_passive_focus_never_arm_or_change_shortcut(self):
        self.assertEqual(self.app.hotkey_text.get(), 'CTRL+ALT+/')
        self.assertEqual(self.app.settings.modifiers, 3)
        self.app._hotkey_focus_in()
        self.app._hotkey_tab_changed()
        self.app._record_key(SimpleNamespace(keycode=65))
        self.assertFalse(self.app.recording)
        self.hotkey.pause.assert_not_called()
        self.assertEqual(self.app.hotkey_text.get(), 'CTRL+ALT+/')

    def test_click_and_chord_save_then_release_focus(self):
        self.app._record_start()
        self.assertTrue(self.app.recording)
        self.assertEqual(self.app.hotkey_text.get(), 'CTRL+ALT+/')
        with patch('script_tts.app.pressed_modifiers', return_value=6), patch.object(self.root, 'focus_set') as unfocus:
            self.app._record_key(SimpleNamespace(keycode=0xBE))
        self.assertEqual(self.app.hotkey_text.get(), 'CTRL+SHIFT+.')
        self.assertFalse(self.app.recording)
        unfocus.assert_called_once()
        self.hotkey.suppress_until_release.assert_called_once_with(6, 0xBE)
        self.app._hotkey_focus_in()
        self.app._hotkey_tab_changed()
        self.assertEqual(self.app.hotkey_text.get(), 'CTRL+SHIFT+.')
        self.assertFalse(self.app.recording)

    def test_invalid_key_or_click_elsewhere_does_not_replace_saved_value(self):
        self.app._record_start()
        with patch('script_tts.app.pressed_modifiers', return_value=0):
            self.app._record_key(SimpleNamespace(keycode=65))
        self.assertEqual(self.app.hotkey_text.get(), 'CTRL+ALT+/')
        self.app._hotkey_outside_click(SimpleNamespace(widget=self.app.current_label))
        self.assertFalse(self.app.recording)
        self.assertEqual(self.app.hotkey_text.get(), 'CTRL+ALT+/')


if __name__ == '__main__':
    unittest.main()
