"""tools/load_test.py: the memory peak of a level only counts samples taken during that level."""
from datetime import datetime, timezone

from tools.load_test import memory_peaks


def test_memory_peak_uses_only_samples_inside_the_window(tmp_path):
    csv_file = tmp_path / "memory.csv"
    csv_file.write_text(
        "time,api_mb,worker_mb,ffmpeg_mb,ffmpeg_count,total_mb\n"
        "2026-10-17T10:00:00.0000000Z,80,900,0,0,980\n"
        "2026-10-17T10:00:05.1234567Z,85,2500,300,2,2885\n"
        "2026-10-17T10:00:10.0000000Z,90,4000,0,0,4090\n", encoding="utf-8")
    start = datetime(2026, 10, 17, 10, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 10, 17, 10, 0, 6, tzinfo=timezone.utc)
    peaks = memory_peaks(csv_file, start, end)
    assert peaks == {"api_mb": 85.0, "worker_mb": 2500.0, "ffmpeg_mb": 300.0, "total_mb": 2885.0, "samples": 2}
    assert memory_peaks(csv_file, end.replace(minute=5), end.replace(minute=6)) is None
    assert memory_peaks(tmp_path / "missing.csv", start, end) is None
