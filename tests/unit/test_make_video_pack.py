"""tools/make_video_pack.py: every video goes in with its json/srt, and the zip may never be inside the repo."""
import zipfile

import pytest

from tools.make_video_pack import ROOT, make_pack


def test_pack_has_videos_side_files_and_an_index(tmp_path):
    job = tmp_path / "outputs" / "run1" / "core01"
    job.mkdir(parents=True)
    for name in ("video_16x9.mp4", "video_9x16.mp4", "timeline.json", "captions.srt", "manifest.json", "job.json"):
        (job / name).write_bytes(b"x" * 10)
    (tmp_path / "outputs" / "run1" / "failed_job").mkdir()  # no video: not packed
    info = make_pack(tmp_path / "outputs", tmp_path / "pack" / "video_pack.zip")
    names = set(zipfile.ZipFile(info["zip"]).namelist())
    assert names == {f"run1/core01/{n}" for n in ("video_16x9.mp4", "video_9x16.mp4", "timeline.json",
                                                  "captions.srt", "manifest.json", "job.json")} | {"pack_index.csv"}
    assert info["videos"] == 2


def test_pack_inside_the_repo_is_refused(tmp_path):
    with pytest.raises(ValueError, match="inside the repository"):
        make_pack(tmp_path, ROOT / "video_pack.zip")
