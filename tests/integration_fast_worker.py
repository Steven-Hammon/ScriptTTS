"""Verify a GUI crash cannot leave the optimized CUDA worker orphaned."""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parent.parent


def main():
    source = "import json,os,time; from script_tts.fast_engine import FastQwenEngine; engine=FastQwenEngine(lambda _:None); engine._start(); print(json.dumps({'owner':os.getpid(),'worker':engine.process.pid}),flush=True); time.sleep(60)"
    client = subprocess.Popen([sys.executable, '-c', source], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, creationflags=0x08000000)
    owner, worker, descendants = None, None, []
    try:
        line = client.stdout.readline()
        if not line:
            raise RuntimeError(client.stderr.read())
        state = json.loads(line)
        owner, worker = psutil.Process(state['owner']), psutil.Process(state['worker'])
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            descendants = worker.children(recursive=True)
            if descendants:
                break
            time.sleep(0.05)
        assert descendants, 'Did not observe the real Python child behind the venv launcher'
        tracked = [worker] + descendants
        owner.kill()  # simulate an unexpected GUI exit
        client.wait(timeout=5)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and any(process.is_running() for process in tracked):
            time.sleep(0.05)
        assert not any(process.is_running() for process in tracked), 'Worker survived its owning process'
        print('PASS crash cleanup: venv launcher and real Qwen worker both exited')
    finally:
        for process in descendants + [worker, owner]:
            if process is not None:
                try:
                    if process.is_running():
                        process.kill()
                except psutil.NoSuchProcess:
                    pass
        if client.poll() is None:
            client.kill()
        client.wait(timeout=5)


if __name__ == '__main__':
    main()
