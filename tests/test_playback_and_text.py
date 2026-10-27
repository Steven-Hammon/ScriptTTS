import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

from script_tts.audio import AudioPlayer
from script_tts.config import Voice
from script_tts.engine import Cancelled
from script_tts.reader import Reader
from script_tts.textprep import TextPreparer, spoken_draft
from test_reader import FakeAudio, FakeDocument, ImmediateEngine, until


class FakeStream:
    def __init__(self, **kwargs):
        self.callback = kwargs['callback']
        self.time, self.active = 0.0, False

    def start(self):
        self.active = True

    def abort(self):
        self.active = False

    def close(self):
        self.active = False


class DeviceClockTests(unittest.TestCase):
    def test_queued_samples_do_not_mean_audible_or_finished(self):
        player = AudioPlayer(sd=SimpleNamespace(OutputStream=FakeStream))
        player.play(np.ones(240, dtype=np.float32), 24000)
        stream = player.stream
        stream.time = 9.9
        output = np.empty((240, 1), dtype=np.float32)
        stream.callback(output, 240, SimpleNamespace(outputBufferDacTime=10.0), None)
        self.assertEqual(player.position, 240)  # all samples already queued
        self.assertFalse(player.started())
        self.assertTrue(player.active())
        stream.time = 10.005
        self.assertTrue(player.started())
        self.assertTrue(player.active())
        stream.time = 10.011
        self.assertFalse(player.active())
        player.stop()


class DelayedAudio(FakeAudio):
    def __init__(self):
        super().__init__()
        self.audible = False

    def play(self, audio, rate):
        self.audible = False
        super().play(audio, rate)

    def started(self):
        return self.audible


class SynchronizationTests(unittest.TestCase):
    def test_tracking_failure_does_not_drop_remaining_speech_or_finish_early(self):
        from script_tts.documents import PositionError
        doc, audio, events = FakeDocument(), FakeAudio(), []
        doc.text = 'First sentence. Second sentence. Third sentence. Fourth sentence. Final sentence.'
        doc.place = Mock(side_effect=[None, None, PositionError('Marker unavailable'), None, None])
        reader = Reader(lambda *event: events.append(event), capture=lambda _: doc, engine=ImmediateEngine(), audio=audio, com=False)
        try:
            reader.toggle(1, Voice(model='kokoro', cues_enabled=False))
            for expected in range(1, 6):
                until(lambda: len(audio.played) == expected)
                time.sleep(0.025)
                self.assertIsNotNone(reader.session)
                self.assertFalse(reader.session.cancel.is_set())
                self.assertFalse(any(kind == 'ready' and str(value).startswith('Finished') for kind, value in events))
                audio.running = False
            until(lambda: reader.session is None)
            self.assertEqual(len(audio.played), 5)
            self.assertIn(('tracking_warning', 'Marker unavailable'), events)
            self.assertTrue(any(kind == 'ready' and str(value).startswith('Finished') for kind, value in events))
        finally:
            reader.close()

    def test_marker_waits_for_each_clip_to_become_audible(self):
        doc, audio = FakeDocument(), DelayedAudio()
        reader = Reader(lambda *_: None, capture=lambda _: doc, engine=ImmediateEngine(), audio=audio, com=False)
        try:
            reader.toggle(1, Voice(model='kokoro', cues_enabled=False))
            until(lambda: len(audio.played) == 1)
            time.sleep(0.05)
            self.assertEqual(doc.markers, [])
            audio.audible = True
            until(lambda: doc.markers == ['First sentence.'])
            time.sleep(0.06)  # next audio is prepared, but current audio still plays
            self.assertEqual(doc.markers, ['First sentence.'])
            self.assertEqual(len(audio.played), 1)
            audio.running = False
            until(lambda: len(audio.played) == 2)
            self.assertEqual(doc.markers, ['First sentence.'])
            audio.audible = True
            until(lambda: len(doc.markers) == 2)
        finally:
            reader.close()

    def test_cues_do_not_overlap_speech(self):
        audio = FakeAudio()
        cues = []
        def cue(name):
            cues.append(name)
            audio.running = True
        audio.cue = cue
        reader = Reader(lambda *_: None, capture=lambda _: FakeDocument(), engine=ImmediateEngine(), audio=audio, com=False)
        try:
            reader.toggle(1, Voice(model='kokoro'))
            until(lambda: cues == ['Starting.wav'])
            time.sleep(0.07)
            self.assertEqual(audio.played, [])
            audio.running = False
            until(lambda: bool(audio.played))
            reader.toggle(1, Voice(model='kokoro'))
            until(lambda: cues == ['Starting.wav', 'Stopped.wav'])
        finally:
            reader.close()

    def test_text_preparation_overlaps_previous_tts_generation(self):
        tts_started, next_text_started, release = threading.Event(), threading.Event(), threading.Event()
        engine, preparer = Mock(), Mock()
        def render(text, *_):
            tts_started.set()
            release.wait(2)
            return text, 24000
        def prepare(text, *_):
            if text.startswith('Second'):
                next_text_started.set()
                release.wait(2)
            return text
        engine.synthesize.side_effect = render
        preparer.prepare.side_effect = prepare
        reader = Reader(lambda *_: None, capture=lambda _: FakeDocument(), engine=engine, preparer=preparer, audio=FakeAudio(), com=False)
        try:
            reader.toggle(1, Voice(model='kokoro'))
            self.assertTrue(tts_started.wait(2))
            self.assertTrue(next_text_started.wait(2))
        finally:
            release.set()
            reader.close()

    def test_load_models_waits_for_both_models_without_playing_audio(self):
        release = threading.Event()
        preparer = Mock()
        preparer.warmup.side_effect = lambda *_: release.wait(2)
        events, audio = [], FakeAudio()
        reader = Reader(lambda *event: events.append(event), capture=Mock(), engine=ImmediateEngine(), preparer=preparer, audio=audio, com=False)
        try:
            reader.load_models(Voice(model='kokoro', ollama_enabled=True))
            until(lambda: ('busy', True) in events)
            time.sleep(0.05)
            self.assertFalse(any(kind == 'ready' for kind, _ in events))
            release.set()
            until(lambda: any(kind == 'ready' for kind, _ in events))
            self.assertEqual(audio.played, [])
            reader.capture.assert_not_called()
        finally:
            release.set()
            reader.close()


class TextTests(unittest.TestCase):
    def test_inflight_ollama_request_can_be_cancelled_without_waiting_for_tokens(self):
        import asyncio
        from script_tts.textprep import OllamaClient
        started, closed, cancel = threading.Event(), threading.Event(), threading.Event()
        class Response:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *_):
                closed.set()
            def raise_for_status(self):
                pass
            async def aiter_lines(self):
                started.set()
                await asyncio.sleep(60)
                yield ''
        class Client:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *_):
                pass
            def stream(self, *_args, **_kwargs):
                return Response()
        results = []
        def run():
            try:
                OllamaClient()._request('/api/chat', {}, cancel)
            except Cancelled:
                results.append('cancelled')
        with patch('httpx.AsyncClient', return_value=Client()):
            worker = threading.Thread(target=run, daemon=True)
            worker.start()
            self.assertTrue(started.wait(2))
            cancel.set()
            worker.join(1)
        self.assertEqual(results, ['cancelled'])
        self.assertTrue(closed.is_set())

    def test_sentence_case_and_spoken_values(self):
        self.assertEqual(spoken_draft('JAMES PAID $1,234.50.'), 'James paid one thousand, two hundred and thirty-four dollars and fifty cents.')
        self.assertEqual(spoken_draft('IT IS THE 21st. I HAVE 2.05 AND 50%.'), 'It is the twenty-first. I have two point zero five and fifty percent.')
        self.assertEqual(spoken_draft('TEMPERATURE: -12.'), 'Temperature: minus twelve.')
        self.assertEqual(spoken_draft('VALUE $1.234.'), 'Value one point two three four dollars.')
        self.assertEqual(spoken_draft('CODE 007.'), 'Code zero zero seven.')

    def test_ollama_receives_original_and_draft_and_caches_by_model(self):
        client = Mock()
        client.rewrite.return_value = 'James has twelve apples.'
        prepare = TextPreparer(client)
        voice = Voice(ollama_enabled=True, ollama_model='local-model')
        result = prepare.prepare('JAMES HAS 12 APPLES.', voice, threading.Event())
        self.assertEqual(result, 'James has twelve apples.')
        self.assertEqual(client.rewrite.call_args.args[:2], ('JAMES HAS 12 APPLES.', 'James has twelve apples.'))
        prepare.prepare('JAMES HAS 12 APPLES.', voice, threading.Event())
        self.assertEqual(client.rewrite.call_count, 1)

    def test_cancelled_text_is_never_cached(self):
        client, event = Mock(), threading.Event()
        def rewrite(*_):
            event.set()
            return 'A stale response.'
        client.rewrite.side_effect = rewrite
        preparer = TextPreparer(client)
        with self.assertRaises(Cancelled):
            preparer.prepare('SOURCE', Voice(ollama_enabled=True, ollama_model='local'), event)
        self.assertFalse(preparer.cache)


class ReaderUiTests(unittest.TestCase):
    def test_loading_colour_ready_colour_and_changed_model_invalidate_warmup(self):
        import tkinter as tk
        from tkinter import ttk
        from script_tts.app import App
        from script_tts.config import Settings
        root = tk.Tk()
        root.withdraw()
        hotkey = Mock()
        hotkey.register.return_value = (True, '')
        with patch('script_tts.app.Settings.load', return_value=Settings()), patch.object(Settings, 'save'), patch('script_tts.app.Reader') as reader, patch('script_tts.app.GlobalHotkey', return_value=hotkey), patch('script_tts.app.OllamaClient'):
            app = App(root)
            try:
                app._set_status('Loading models', 'loading')
                self.assertEqual(str(app.status_label.cget('style')), 'Loading.TLabel')
                self.assertEqual(ttk.Style().lookup('Loading.TLabel', 'foreground'), '#8b1e22')
                app._blink_status()
                self.assertEqual(str(app.status_label.cget('style')), 'Status.TLabel')
                app._set_status('Ready', 'ready')
                self.assertEqual(str(app.status_label.cget('style')), 'Ready.TLabel')
                self.assertEqual(ttk.Style().lookup('Ready.TLabel', 'foreground'), '#14532d')
                app._models_warming = True
                app.normalize.set(False)
                reader.return_value.stop.assert_called_with(announce=False)
                self.assertFalse(app._models_warming)
                self.assertFalse(app._loading)
            finally:
                app.close()
