"""TEMPORARY fake pipeline (Phase 2).

It walks through the real stage names, sleeps a little at each one, updates progress,
and saves a small text file. Later phases replace this with the real pipeline.
"""
import json
import time
from typing import Any

from app.config import get_settings
from app.jobs import set_progress
from app.storage import get_storage, output_key

# (stage name, progress % when that stage starts)
STAGES = [("intake", 10), ("transcribe", 30), ("understand", 50), ("footage", 70), ("render", 90)]


def run_fake_pipeline(job_id: str, request: dict[str, Any], input_type: str) -> list[str]:
    """Returns the list of output filenames it saved."""
    delay = get_settings().FAKE_STAGE_SECONDS
    for stage, pct in STAGES:
        set_progress(job_id, pct, stage)
        time.sleep(delay)

    summary = {
        "job_id": job_id,
        "input_type": input_type,
        "aspect_ratios": request.get("aspect_ratios"),
        "note": "Fake pipeline output (Phase 2). Real videos come in later phases.",
    }
    get_storage().put_bytes(output_key(job_id, "result.txt"), json.dumps(summary, indent=2).encode())
    return ["result.txt"]
