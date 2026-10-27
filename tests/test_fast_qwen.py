from __future__ import annotations

import threading
import unittest
from unittest.mock import Mock, patch

from script_tts.config import Voice
from script_tts.fast_engine import FastQwenEngine, qwen_speaker
from script_tts.fast_worker import generate_audio
from script_tts.reader import Reader
from test_reader import FakeAudio, FakeDocument, ImmediateEngine, until


class FastQwenTests(unittest.TestCase):
    def test_preset_selection_and_independent_kokoro_setting(self):
        self.assertEqual(qwen_speaker(Voice(description='20 year old woman')), 'vivian')
        self.assertEqual(qwen_speaker(Voice(description='young man')), 'aiden')
        self.assertEqual(qwen_speaker(Voice(qwen_speaker='Serena', preset='af_heart')), 'serena')
        self.assertEqual(qwen_speaker(Voice(language='Japanese')), 'ono_anna')
        with self.assertRaises(ValueError):
            qwen_speaker(Voice(qwen_speaker='missing'))

    def test_worker_uses_preset_without_reference_or_unsupported_instruction(self):
        import numpy as np
        model = Mock()
        def stream():
            yield np.ones(100, dtype=np.float32), 24000, {}
        model.generate_custom_voice_streaming.return_value = stream()
        result = generate_audio(model, {'text': 'Test.', 'speaker': 'vivian', 'language': 'English'}, threading.Event())
        self.assertEqual(result['type'], 'audio')
        arguments = model.generate_custom_voice_streaming.call_args.kwargs
        self.assertEqual(arguments['speaker'], 'vivian')
        self.assertNotIn('ref_audio', arguments)
        self.assertNotIn('instruct', arguments)

    def test_cancel_closes_stream_and_discards_partial_audio(self):
        import numpy as np
        cancel, closed = threading.Event(), threading.Event()
        def stream():
            try:
                yield np.ones(100, dtype=np.float32), 24000, {}
                cancel.set()
                yield np.ones(100, dtype=np.float32), 24000, {}
            finally:
                closed.set()
        model = Mock()
        model.generate_custom_voice_streaming.return_value = stream()
        result = generate_audio(model, {'text': 'Test.', 'speaker': 'vivian', 'language': 'English'}, cancel)
        self.assertIsNone(result)
        self.assertTrue(closed.is_set())

    def test_reader_does_not_validate_a_wav_for_preset_model(self):
        audio = FakeAudio()
        reader = Reader(lambda *_: None, capture=lambda _: FakeDocument(), engine=ImmediateEngine(), audio=audio, com=False)
        try:
            with patch('script_tts.reader.validate_voice') as validate:
                reader.toggle(1, Voice(model='qwen-preset-fast'))
                until(lambda: bool(audio.played))
                validate.assert_not_called()
        finally:
            reader.close()

    def test_cancel_is_forwarded_without_waiting_for_the_worker(self):
        from script_tts.engine import Cancelled
        engine = FastQwenEngine(lambda _: None)
        event = threading.Event()
        messages = []
        def send(message):
            messages.append(message)
            if message['type'] == 'generate':
                event.set()
        with patch.object(engine, '_start'), patch.object(engine, '_send', side_effect=send):
            with self.assertRaises(Cancelled):
                engine.synthesize('Test.', Voice(model='qwen-preset-fast'), event)
        self.assertEqual([message['type'] for message in messages], ['generate', 'cancel'])


if __name__ == '__main__':
    unittest.main()
