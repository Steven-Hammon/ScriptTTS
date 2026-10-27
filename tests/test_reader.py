from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import bootstrap
from script_tts.config import MODEL_REVISION, Settings, Voice
from script_tts.documents import UIADocument
from script_tts.engine import Cancelled, KokoroEngine, QwenEngine, SpeechEngine, kokoro_voice
from script_tts.hotkey import label
from script_tts.reader import Reader
from script_tts.sentences import Sentence, speech_chunks, split_sentences


def until(predicate, seconds=2):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("Timed out waiting for reader state")


class SentenceTests(unittest.TestCase):
    def test_source_offsets_and_abbreviations(self):
        text = '  Dr. Smith paid $3.14. “Ready?” Yes!\r\nA heading\nNext sentence.'
        spans = split_sentences(text)
        self.assertEqual([s.text for s in spans], ['Dr. Smith paid $3.14.', '“Ready?”', 'Yes!', 'A heading', 'Next sentence.'])
        self.assertTrue(all(text[s.start:s.end] == s.text for s in spans))

    def test_repeated_sentences_unicode_and_mid_sentence(self):
        text = 'remaining words. Repeat. Repeat. \U0001f600 Smile! \u4f60\u597d\u3002\u518d\u89c1\u3002'
        spans = split_sentences(text)
        self.assertEqual([s.text for s in spans], ['remaining words.', 'Repeat.', 'Repeat.', '\U0001f600 Smile!', '\u4f60\u597d\u3002', '\u518d\u89c1\u3002'])
        self.assertLess(spans[1].start, spans[2].start)

    def test_long_sentence_chunking_preserves_words(self):
        text = ('Here is a fairly long phrase, ' * 60).strip()
        chunks = speech_chunks(text)
        self.assertTrue(all(len(s) <= 320 for s in chunks))
        self.assertEqual(' '.join(chunks), text)

    def test_pdf_wrapping_does_not_split_a_sentence(self):
        text = 'The sentence wraps\nonto another line. Another sentence.'
        self.assertEqual([s.text for s in split_sentences(text, line_breaks=False)], ['The sentence wraps\nonto another line.', 'Another sentence.'])


class SetupTests(unittest.TestCase):
    def test_internal_downloader_requires_previous_consent(self):
        with patch('sys.argv', ['bootstrap.py', '--download-model', '--model', 'qwen-small']), patch.object(bootstrap, 'has_consent', return_value=False), patch.object(bootstrap, 'download_model') as download:
            with self.assertRaises(RuntimeError):
                bootstrap.main()
            download.assert_not_called()

    def test_decline_and_eof_are_never_acceptance(self):
        self.assertFalse(bootstrap.ask_consent(lambda _: '2', lambda _: None))
        def eof(_):
            raise EOFError
        self.assertFalse(bootstrap.ask_consent(eof, lambda _: None))

    def test_only_explicit_one_accepts(self):
        answers = iter(['yes', '', '1'])
        self.assertTrue(bootstrap.ask_consent(lambda _: next(answers), lambda _: None))

    def test_partial_model_is_not_ready(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = root / 'manifest.json'
            manifest.write_text(json.dumps({'revision': MODEL_REVISION, 'files': {'model.safetensors': 20, 'speech_tokenizer/model.safetensors': 10}}))
            (root / 'model.safetensors').write_bytes(b'partial')
            with patch.object(bootstrap, 'MANIFEST', manifest), patch.object(bootstrap, 'MODEL_PATH', root):
                self.assertFalse(bootstrap.model_ready())

    def test_decline_does_not_write_or_install(self):
        with patch.object(bootstrap, 'has_consent', return_value=False), patch.object(bootstrap, 'runtime_ready', return_value=False), patch.object(bootstrap, 'model_ready', return_value=False), patch.object(bootstrap, 'ask_consent', return_value=False), patch.object(bootstrap, 'write_json') as write, patch.object(bootstrap, 'install_runtime') as install, patch.object(bootstrap, 'download_model') as download, patch('subprocess.Popen') as launch:
            self.assertEqual(bootstrap.main(), 0)
            write.assert_not_called()
            install.assert_not_called()
            download.assert_not_called()
            launch.assert_not_called()

    def test_hotkey_physical_slash(self):
        self.assertEqual(label(6, 0xBF), 'Ctrl+Shift+/')

    def test_settings_roundtrip_and_corruption(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'settings.json'
            settings = Settings(wav='C:/Voice/sample.wav', transcript='A transcript.')
            settings.save(path)
            self.assertEqual(Settings.load(path), settings)
            path.write_text('{bad json')
            self.assertEqual(Settings.load(path), Settings())


class FakeDocument:
    text = 'First sentence. Second sentence.'
    name = 'test document'

    def __init__(self):
        self.markers = []
        self.focus = True

    def check_foreground(self):
        if not self.focus:
            raise RuntimeError('Switched windows')

    def place(self, sentence):
        self.markers.append(sentence.text)

    def finish(self):
        pass


class FakeAudio:
    def __init__(self):
        self.played = []
        self.running = False

    def play(self, audio, rate):
        self.played.append(audio)
        self.running = True

    def stop(self):
        self.running = False

    def active(self):
        return self.running


class BlockingEngine:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.cancel = None

    def synthesize(self, text, voice, cancel):
        self.cancel = cancel
        self.started.set()
        self.release.wait(timeout=3)
        # Intentionally return stale output: the coordinator must discard it.
        return text, 24000


class ImmediateEngine:
    def synthesize(self, text, voice, cancel):
        return text, 24000


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.doc, self.audio, self.events = FakeDocument(), FakeAudio(), []
        self.validator = patch('script_tts.reader.validate_voice')
        self.validator.start()
        self.reader = None

    def tearDown(self):
        if self.reader:
            self.reader.close()
        self.validator.stop()

    def create(self, engine):
        self.reader = Reader(lambda *args: self.events.append(args), capture=lambda _: self.doc, engine=engine, audio=self.audio, com=False)
        return self.reader

    def test_stop_during_generation_discards_audio_and_does_not_move_caret(self):
        engine = BlockingEngine()
        reader = self.create(engine)
        reader.toggle(1, Voice())
        self.assertTrue(engine.started.wait(timeout=2))
        reader.toggle(1, Voice())
        until(lambda: engine.cancel.is_set())
        engine.release.set()
        time.sleep(0.08)
        self.assertEqual(self.audio.played, [])
        self.assertEqual(self.doc.markers, [])

    def test_markers_follow_playback_and_stop_retains_current_sentence(self):
        reader = self.create(ImmediateEngine())
        reader.toggle(1, Voice())
        until(lambda: len(self.audio.played) == 1)
        self.assertEqual(self.doc.markers, ['First sentence.'])
        self.audio.running = False
        until(lambda: len(self.audio.played) == 2)
        reader.toggle(1, Voice())
        until(lambda: reader.session is None)
        self.assertEqual(self.doc.markers, ['First sentence.', 'Second sentence.'])
        self.assertFalse(self.audio.running)

    def test_switching_windows_cancels_generation(self):
        engine = BlockingEngine()
        reader = self.create(engine)
        reader.toggle(1, Voice())
        self.assertTrue(engine.started.wait(timeout=2))
        self.doc.focus = False
        until(lambda: engine.cancel.is_set())
        engine.release.set()
        self.assertEqual(self.audio.played, [])

    def test_long_sentence_keeps_one_marker(self):
        self.doc.text = ('word ' * 150).strip() + '.'
        reader = self.create(ImmediateEngine())
        reader.toggle(1, Voice())
        until(lambda: len(self.audio.played) == 1)
        self.audio.running = False
        until(lambda: len(self.audio.played) == 2)
        self.assertEqual(len(self.doc.markers), 1)

    def test_prefetch_runs_during_playback_without_advancing_marker(self):
        from unittest.mock import Mock
        engine = Mock()
        engine.synthesize.side_effect = lambda text, *_: (text, 24000)
        reader = self.create(engine)
        reader.toggle(1, Voice())
        until(lambda: engine.synthesize.call_count == 2)
        self.assertTrue(self.audio.running)
        self.assertEqual(self.audio.played, ['First sentence.'])
        self.assertEqual(self.doc.markers, ['First sentence.'])

    def test_kokoro_does_not_require_a_wav(self):
        reader = self.create(ImmediateEngine())
        with patch('script_tts.reader.validate_voice') as validate:
            reader.toggle(1, Voice(model='kokoro'))
            until(lambda: len(self.audio.played) == 1)
            validate.assert_not_called()


class EngineTests(unittest.TestCase):
    def test_kokoro_description_and_explicit_preset(self):
        self.assertEqual(kokoro_voice(Voice(model='kokoro')), ('af_heart', 'en-us'))
        self.assertEqual(kokoro_voice(Voice(description='British male')), ('bm_george', 'en-gb'))
        self.assertEqual(kokoro_voice(Voice(description='female', preset='am_michael')), ('am_michael', 'en-us'))

    def test_kokoro_speed_reaches_the_backend_at_both_limits(self):
        from unittest.mock import Mock
        engine = KokoroEngine(lambda _: None)
        engine.model = Mock()
        engine.model.voices = {'af_heart': None}
        engine.model.create.return_value = ('audio', 24000)
        for speed in (0.5, 1.0, 2.0):
            engine.synthesize('Test.', Voice(model='kokoro', speed=speed), threading.Event())
            self.assertEqual(engine.model.create.call_args.kwargs['speed'], speed)
        with self.assertRaises(ValueError):
            engine.synthesize('Test.', Voice(model='kokoro', speed=2.1), threading.Event())

    def test_model_switch_unloads_previous_backend(self):
        from unittest.mock import Mock
        old, new = Mock(), Mock()
        engine = SpeechEngine(lambda _: None)
        with patch('script_tts.engine.model_ready', return_value=True), patch('script_tts.engine.QwenEngine', return_value=old), patch('script_tts.engine.KokoroEngine', return_value=new):
            engine.synthesize('Test.', Voice(), threading.Event())
            engine.synthesize('Test.', Voice(model='kokoro'), threading.Event())
        old.unload.assert_called_once()
        new.synthesize.assert_called_once()

    def test_cancel_hook_is_removed_when_generation_raises(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'sample.wav'
            path.write_bytes(b'placeholder')
            engine = QwenEngine(lambda _: None)
            cancel = threading.Event()
            hook = Mock()
            talker = Mock()
            talker.register_forward_pre_hook.return_value = hook
            model = Mock()
            model.model = SimpleNamespace(talker=talker)
            model.generate_voice_clone.side_effect = Cancelled()
            engine.model = model
            with patch('script_tts.engine.validate_voice'):
                with self.assertRaises(Cancelled):
                    engine.synthesize('Test.', Voice(wav=str(path)), cancel)
            hook.remove.assert_called_once()


if __name__ == '__main__':
    unittest.main()
