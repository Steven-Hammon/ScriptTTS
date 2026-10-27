"""Generate real local audio with an already accepted/installed model.

No document access, model downloads or license acceptance occur here.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from script_tts.config import ROOT, Settings, model_ready
from script_tts.engine import Cancelled, SpeechEngine


def main(key):
    import numpy as np
    import soundfile as sf

    assert model_ready(key), f'{key} must be installed using the agreement prompt first'
    engine = SpeechEngine(lambda status: print(status, flush=True))
    voice = replace(Settings.load().voice(), model=key, speed=1.0)
    output = ROOT / 'tests' / 'output'
    output.mkdir(exist_ok=True)
    results = []
    for speed in ((1.0, 0.5, 2.0) if key == 'kokoro' else (1.0,)):
        start = time.monotonic()
        audio, rate = engine.synthesize('This is a short test of the sentence reader.', replace(voice, speed=speed), threading.Event())
        elapsed = time.monotonic() - start
        assert rate > 0 and len(audio) > rate * 0.2
        assert np.isfinite(audio).all() and np.max(np.abs(audio)) > 0.001
        path = output / f'{key}-{speed:.1f}x.wav'
        sf.write(path, audio, rate)
        item = {'model': key, 'speed': speed, 'generation_seconds': round(elapsed, 3), 'audio_seconds': round(len(audio) / rate, 3), 'path': str(path)}
        results.append(item)
        print(json.dumps(item), flush=True)
    if key == 'kokoro':
        assert results[1]['audio_seconds'] > results[2]['audio_seconds'] * 2.5, 'Speed control did not change duration as expected'
    else:
        if key == 'qwen-preset-fast':
            for speaker in ('Vivian', 'Serena'):
                start = time.monotonic()
                audio, rate = engine.synthesize('The next sentence is prepared while the current sentence plays.', replace(voice, qwen_speaker=speaker), threading.Event())
                elapsed = time.monotonic() - start
                assert np.isfinite(audio).all() and np.max(np.abs(audio)) > 0.001
                item = {'model': key, 'speaker': speaker, 'warm_generation_seconds': round(elapsed, 3), 'audio_seconds': round(len(audio) / rate, 3)}
                results.append(item)
                print(json.dumps(item), flush=True)
        cancel = threading.Event()
        timer = threading.Timer(0.25, cancel.set)
        timer.start()
        try:
            engine.synthesize('This is a longer passage that must be cancelled before it can finish generating speech. ' * 4, voice, cancel)
        except Cancelled:
            print('PASS live model cancellation', flush=True)
        else:
            raise AssertionError('Qwen did not cancel')
        finally:
            timer.cancel()
        if key == 'qwen-preset-fast':
            audio, rate = engine.synthesize('Reading can resume after stopping.', voice, threading.Event())
            assert len(audio) > rate * 0.2 and np.isfinite(audio).all()
            print('PASS resume after live cancellation', flush=True)
    (output / f'{key}-metrics.json').write_text(json.dumps(results, indent=2), encoding='utf-8')
    if engine.backend is not None:
        engine.backend.unload()


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else 'kokoro')
