"""Resilience test helper: the normal worker (app.worker), but some real footage downloads arrive corrupted.

Fault injection lives ONLY in this test tool, never in the app. The first --count downloads of
--provider are really downloaded, then cut to their first 4 KB (a broken MP4 that ffprobe cannot
read), as if the connection had dropped. The pipeline must notice (ffprobe check), skip that clip
and use the next candidate, without failing the job. Every injected fault is logged as
"FAULT INJECTION" so the log proves which clip was broken.

Stop the normal worker first (only one worker should claim the test job), then:
    .venv\\Scripts\\python tools\\fault_worker.py --provider pixabay_video --count 1
"""
import argparse
import logging
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.pipeline.providers import REGISTRY  # noqa: E402

log = logging.getLogger("tools.fault_worker")


def corrupt_downloads(provider: str, count: int, keep_bytes: int = 4096) -> dict:
    """Wrap REGISTRY[provider].fetch so the first `count` downloads are truncated. Returns the live counter."""
    cls = REGISTRY[provider]
    real_fetch = cls.fetch
    state = {"left": count, "corrupted": []}
    lock = threading.Lock()

    def fetch(self, candidate, dest_dir):
        path = real_fetch(self, candidate, dest_dir)
        with lock:
            if state["left"] <= 0:
                return path
            state["left"] -= 1
        size = path.stat().st_size
        path.write_bytes(path.read_bytes()[:keep_bytes])
        state["corrupted"].append(candidate.uid)
        log.warning("FAULT INJECTION: download of %s cut from %d to %d bytes", candidate.uid, size, keep_bytes,
                    extra={"fields": {"fault": "corrupted_download", "clip": candidate.uid}})
        return path

    cls.fetch = fetch
    return state


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the worker with corrupted footage downloads (test only).")
    parser.add_argument("--provider", default="pixabay_video", choices=sorted(REGISTRY))
    parser.add_argument("--count", type=int, default=1, help="how many downloads to corrupt (default 1)")
    args = parser.parse_args(argv)
    corrupt_downloads(args.provider, args.count)
    from app import worker

    worker.main()


if __name__ == "__main__":
    main()
