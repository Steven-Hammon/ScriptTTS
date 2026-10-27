"""Opt-in real audio / local Ollama check with a synthetic, read-only source."""
from dataclasses import replace
from pathlib import Path
import json
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from script_tts.audio import AudioPlayer
from script_tts.config import Settings
from script_tts.reader import Reader
from script_tts.textprep import OllamaClient, choose_default_model


class TestDocument:
    name = 'Synthetic timing test'
    text = 'JAMES PAID $1,234.50 IN 2024. ANNA READ THE 21st PAGE. I OWE 50% OF 2.05.'

    def __init__(self, audio):
        self.audio = audio
        self.markers = []

    def check_foreground(self):
        pass

    def place(self, sentence):
        assert self.audio.started(), 'Cursor moved before audio reached the device'
        assert self.audio.active(), 'Cursor moved after the clip had already ended'
        drift = self.audio.stream.time - self.audio.start_dac
        self.markers.append({'source': sentence.text, 'marker_delay_seconds': round(drift, 4)})
        print('MARKER', json.dumps(self.markers[-1]), flush=True)

    def finish(self):
        pass


def main():
    audio = AudioPlayer()
    document = TestDocument(audio)
    loaded, finished = threading.Event(), threading.Event()
    errors, spoken = [], []
    def notify(kind, value):
        if kind in {'loading', 'ready', 'error', 'spoken'}:
            print(kind.upper(), value, flush=True)
        if kind == 'ready' and str(value).startswith('Models loaded'):
            loaded.set()
        if kind == 'ready' and str(value).startswith('Finished'):
            finished.set()
        if kind == 'error':
            errors.append(value)
            loaded.set()
            finished.set()
        if kind == 'spoken':
            spoken.append(value)
    reader = Reader(notify, capture=lambda _: document, audio=audio, com=False)
    try:
        model = choose_default_model(OllamaClient().models())
        voice = replace(Settings.load().voice(), model='kokoro', normalize_text=True, ollama_enabled=True, ollama_model=model, cues_enabled=True)
        reader.load_models(voice)
        assert loaded.wait(90), 'Model loading timed out'
        assert not errors, errors
        reader.toggle(1, voice)
        assert finished.wait(90), 'Reading timed out'
        assert not errors, errors
        assert len(document.markers) == 3
        assert 'one thousand' in spoken[0].lower(), spoken[0]
        assert 'fifty cents' in spoken[0].lower(), spoken[0]
        assert max(item['marker_delay_seconds'] for item in document.markers) < 0.15
        # Exercise the explicit stop cue after a fresh start, without waiting
        # for another sentence to be generated.
        reader.toggle(1, voice)
        time.sleep(0.2)
        reader.toggle(1, voice)
        time.sleep(1.7)
        output = Path('tests/output/pipeline-timing.json')
        output.parent.mkdir(exist_ok=True)
        output.write_text(json.dumps({'ollama_model': model, 'spoken': spoken, 'markers': document.markers}, indent=2), encoding='utf-8')
        print('PASS real Ollama + Kokoro pipeline, DAC-timed markers, start/stop cues', flush=True)
    finally:
        reader.close()


if __name__ == '__main__':
    main()
