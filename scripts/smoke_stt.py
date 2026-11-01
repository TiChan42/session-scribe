"""Local integration check using an explicitly synthetic FLAC fixture, in a separate process.

No live audio, Discord connection or cloud calls. Native process resource measurements do not
prove container limits. Prepare fixture using the commands in docs/TESTING.md.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--synthetic-audio", type=Path)
    parser.add_argument("--worker", action="store_true")
    args = parser.parse_args()
    if args.worker:
        import resource

        from sessionscribe.stt import run_handoff

        before = time.monotonic()
        run_handoff(args.root / "input", args.root / "output", args.model)
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        print(
            json.dumps(
                {
                    "elapsed_seconds": round(time.monotonic() - before, 2),
                    "peak_rss_bytes": rss if sys.platform == "darwin" else rss * 1024,
                }
            )
        )
        return
    if args.root.exists() or args.synthetic_audio is None:
        raise ValueError("fresh root and synthetic fixture required")
    import soundfile as sf

    from sessionscribe.control import Controller
    from sessionscribe.demo import synthetic_config
    from sessionscribe.models import Actor, AudioChunk, Limits
    from sessionscribe.recorder import FRAME_BYTES, Frame, Recorder
    from sessionscribe.store import Store
    from sessionscribe.transcript import load_chunks

    config = synthetic_config("meeting")
    store = Store(args.root / "recorder", config.tenant)
    actor = Actor(guild_id="1000", channel_id="1002", user_id="1001")
    holder = {}
    control = Controller(
        config, store, lambda *_: "synthetic-status", lambda _sid: holder["recorder"].close()
    )
    s = control.start(actor, "1003", frozenset({actor.user_id}))
    sid = s.id
    recorder = Recorder(store, sid, Limits(min_free_bytes=0))
    holder["recorder"] = recorder
    control.consent(actor, sid, cloud=False)
    control.resume(actor, sid)
    with sf.SoundFile(args.synthetic_audio) as source:
        if source.samplerate != 48000 or source.channels != 2 or len(source) > 30 * 48000:
            raise ValueError("fixture must be <=30s PCM stereo at 48kHz")
        index = 0
        while len(pcm := source.buffer_read(960, dtype="int16")):
            recorder.accept(Frame(s.owner_id, index * 20, bytes(pcm).ljust(FRAME_BYTES, b"\0")))
            index += 1
    control.stop(actor, sid)
    chunks: list[AudioChunk] = load_chunks(store, sid)
    from sessionscribe.files import SafeFiles

    handoff = SafeFiles(args.root / "input")
    for chunk in chunks:
        handoff.atomic(
            f"sessions/{sid}/{chunk.path}", store.files.read(f"sessions/{sid}/{chunk.path}")
        )
    handoff.json(
        "handoff.json",
        {
            "schema_version": 1,
            "session_id": sid,
            "chunks": [c.model_dump(mode="json") for c in chunks],
        },
    )
    store.close()
    env = os.environ.copy()
    env.update(HF_HUB_OFFLINE="1", OMP_NUM_THREADS="2", OPENBLAS_NUM_THREADS="1")
    run = subprocess.run(
        [
            sys.executable,
            __file__,
            "--root",
            str(args.root),
            "--model",
            str(args.model),
            "--worker",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=180,
    )
    print(run.stdout.strip())
    transcript = json.loads((args.root / "output" / "transcript.json").read_text())
    text = " ".join(s["text"] for s in transcript)
    if "Beschluss" not in text or "Freitag" not in text:
        raise ValueError("synthetic German keywords not recognized")
    print(json.dumps({"synthetic_german_stt": "passed", "text": text}, ensure_ascii=False))


if __name__ == "__main__":
    main()
