from __future__ import annotations

import logging
import queue
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .config import MODELS, Voice
from .documents import DocumentError, PositionError, capture_document
from .engine import AudioPlayer, Cancelled, SpeechEngine, validate_voice
from .sentences import Sentence, speech_chunks, split_sentences
from .textprep import TextPreparer

log = logging.getLogger(__name__)


@dataclass
class Session:
    document: object
    voice: Voice
    units: list[tuple[int, Sentence, str]]
    cancel: threading.Event = field(default_factory=threading.Event)
    index: int = 0
    marked_sentence: int = -1
    prepared: dict = field(default_factory=dict)
    rendered: dict = field(default_factory=dict)
    playing: bool = False
    marker_pending: bool = False


class Reader:
    """STA caret coordinator with separate text-preparation and TTS workers."""

    def __init__(self, notify, *, capture=capture_document, engine=None, audio=None, preparer=None, com=True):
        self.notify, self.capture, self.com = notify, capture, com
        self.worker_context = threading.local()
        self.engine = engine or SpeechEngine(self._model_status)
        self.audio = audio or AudioPlayer()
        self.preparer = preparer or TextPreparer()
        self.commands = queue.Queue()
        self.session = None
        self.warming = None
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tts-inference")
        self.text_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="spoken-text")
        self.thread = threading.Thread(target=self._run, name="document-reader", daemon=True)
        self.thread.start()

    def _model_status(self, message):
        cancel = getattr(self.worker_context, "cancel", None)
        if cancel is None or not cancel.is_set():
            self.notify("model", message)

    def _render(self, text, voice, cancel):
        self.worker_context.cancel = cancel
        try:
            return self.engine.synthesize(text, voice, cancel)
        finally:
            self.worker_context.cancel = None

    def toggle(self, hwnd: int, voice: Voice):
        self.commands.put(("toggle", hwnd, voice))

    def stop(self, announce=True):
        self.commands.put(("stop", announce))

    def load_models(self, voice):
        self.commands.put(("warmup", voice))

    def close(self):
        self.commands.put(("close",))
        self.thread.join(timeout=3)
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.text_executor.shutdown(wait=False, cancel_futures=True)

    def _cancel_warmup(self):
        if self.warming:
            cancel, futures = self.warming
            cancel.set()
            for future in futures:
                future.cancel()
            self.warming = None
            self.notify("busy", False)

    def _cue(self, name, enabled):
        if enabled and hasattr(self.audio, "cue"):
            self.audio.cue(name)

    def _stop(self, message="Stopped · press the hotkey to repeat the current sentence", *, announce=False):
        session = self.session
        if session:
            session.cancel.set()
            for future in list(session.prepared.values()) + list(session.rendered.values()):
                future.cancel()
        self._cancel_warmup()
        self.audio.stop()
        self.session = None
        if announce and session:
            self._cue("Stopped.wav", session.voice.cues_enabled)
        self.notify("status", message)
        self.notify("active", False)

    def _start(self, hwnd, voice):
        self._cancel_warmup()
        self.audio.stop()
        self._cue("Starting.wav", voice.cues_enabled)
        document = self.capture(hwnd)
        if MODELS[voice.model].backend == "qwen":
            validate_voice(voice)
        sentences = split_sentences(document.text, line_breaks=not getattr(document, "pdf", False))
        if not sentences:
            raise DocumentError("There is no text after that position.")
        units = [(i, sentence, part) for i, sentence in enumerate(sentences) for part in speech_chunks(sentence.text)]
        self.session = Session(document, voice, units)
        self.notify("active", True)
        self.notify("loading", f"Preparing speech · {document.name}")

    def _warmup(self, voice):
        self._stop("Loading selected models…")
        if MODELS[voice.model].backend == "qwen":
            validate_voice(voice)
        self.notify("busy", True)
        self.notify("loading", "Loading speech and text models…")
        if hasattr(self.audio, "initialize"):
            self.audio.initialize()
        cancel = threading.Event()
        def warm_text():
            if voice.ollama_enabled and not cancel.is_set():
                self.notify("model", f"Loading Ollama · {voice.ollama_model}…")
            self.preparer.warmup(voice, cancel)
        futures = [self.executor.submit(self._render, "The reader is ready.", voice, cancel), self.text_executor.submit(warm_text)]
        self.warming = cancel, futures

    def _fill_pipeline(self, session):
        # Only the coordinator can touch a document. Preparation is two chunks
        # ahead; TTS is one chunk ahead, on separate single-thread workers.
        for index in range(session.index, min(len(session.units), session.index + 3)):
            if index not in session.prepared:
                session.prepared[index] = self.text_executor.submit(self.preparer.prepare, session.units[index][2], session.voice, session.cancel)
        for index in range(session.index, min(len(session.units), session.index + 2)):
            prepared = session.prepared[index]
            if index not in session.rendered and prepared.done():
                if index != session.index and prepared.exception() is not None:
                    continue  # let the currently audible sentence finish
                session.rendered[index] = self.executor.submit(self._render, prepared.result(), session.voice, session.cancel)

    def _mark_if_audible(self, session):
        if not session.marker_pending or not getattr(self.audio, "started", lambda: True)():
            return
        number, sentence, _ = session.units[session.index]
        tracking_warning = None
        if session.marked_sentence != number:
            try:
                session.document.place(sentence)
                session.marked_sentence = number
            except PositionError as exc:
                tracking_warning = str(exc)
                log.warning("Cursor tracking: %s", exc)
        session.marker_pending = False
        self.notify("sentence", sentence.text)
        self.notify("spoken", session.prepared[session.index].result())
        if tracking_warning:
            self.notify("tracking_warning", tracking_warning)
        else:
            self.notify("ready", f"Reading sentence {number + 1} · {session.document.name}")

    def _tick(self):
        if self.warming:
            cancel, futures = self.warming
            if all(future.done() for future in futures):
                for future in futures:
                    future.result()
                self.warming = None
                self.notify("busy", False)
                if not cancel.is_set():
                    self.notify("ready", "Models loaded · ready to read")
        session = self.session
        if session is None:
            return
        session.document.check_foreground()
        self._fill_pipeline(session)
        if session.playing:
            self._mark_if_audible(session)
            if self.audio.active():
                return
            session.playing = False
            session.prepared.pop(session.index, None)
            session.rendered.pop(session.index, None)
            session.index += 1
            if session.index == len(session.units):
                session.document.finish()
                self._stop("Finished · caret/selection remains at the final sentence")
                self.notify("ready", "Finished · caret/selection remains at the final sentence")
                return
            self._fill_pipeline(session)
        rendered = session.rendered.get(session.index)
        if rendered is None or not rendered.done() or self.audio.active():
            return  # includes the Starting.wav cue and device-buffer drain
        audio, rate = rendered.result()
        if session.cancel.is_set() or self.session is not session:
            return
        self.audio.play(audio, rate)
        session.playing = True
        session.marker_pending = True
        self._mark_if_audible(session)

    def _run(self):
        if self.com:
            import pythoncom
            pythoncom.CoInitialize()
        try:
            while True:
                try:
                    command = self.commands.get(timeout=0.015)
                except queue.Empty:
                    command = None
                try:
                    if command:
                        action = command[0]
                        if action == "close":
                            self._stop()
                            getattr(self.audio, "close", lambda: None)()
                            return
                        if action == "stop":
                            self._stop(announce=command[1])
                        elif action == "toggle":
                            if self.session is not None:
                                self._stop(announce=True)
                            else:
                                self._start(command[1], command[2])
                        elif action == "warmup":
                            self._warmup(command[1])
                    self._tick()
                except Cancelled:
                    self._stop()
                except DocumentError as exc:
                    log.info("Reading stopped: %s", exc)
                    self._stop(str(exc))
                    self.notify("error", str(exc))
                except Exception as exc:
                    log.exception("Reading stopped")
                    self._stop(str(exc))
                    self.notify("error", str(exc))
                if self.com:
                    pythoncom.PumpWaitingMessages()
        finally:
            self.session = None
            if self.com:
                pythoncom.CoUninitialize()
