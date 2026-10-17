"""Zip every test pack video with its timeline.json, captions.srt and manifest.json into video_pack.zip,
OUTSIDE the git repository (videos are large and git-ignored; reviewers get them as one download).

For every folder under testpack/outputs/ that holds a video_*.mp4, the zip gets
    <run>/<input>/video_16x9.mp4, video_9x16.mp4, video_1x1.mp4 (those that exist),
    timeline.json, captions.srt, manifest.json, job.json
plus pack_index.csv at the top (file, bytes, sha256) so anyone can check the files.
Videos are stored as they are (MP4 is already compressed); text files are deflated.

    .venv\\Scripts\\python tools\\make_video_pack.py                     -> ..\\broll-video-pack\\video_pack.zip
    .venv\\Scripts\\python tools\\make_video_pack.py --out D:\\share\\video_pack.zip
"""
import argparse
import csv
import hashlib
import io
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUTS = ROOT / "testpack" / "outputs"
DEFAULT_OUT = ROOT.parent / "broll-video-pack" / "video_pack.zip"
SIDE_FILES = ("timeline.json", "captions.srt", "manifest.json", "job.json")


def pack_files(outputs: Path) -> list[tuple[Path, str]]:
    """(file on disk, name inside the zip) for every job folder with a video, sorted."""
    files = []
    for folder in sorted({p.parent for p in outputs.rglob("video_*.mp4")}):
        rel = folder.relative_to(outputs).as_posix()
        for video in sorted(folder.glob("video_*.mp4")):
            files.append((video, f"{rel}/{video.name}"))
        for name in SIDE_FILES:
            if (folder / name).is_file():
                files.append((folder / name, f"{rel}/{name}"))
    return files


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def make_pack(outputs: Path, out: Path) -> dict:
    out = out.resolve()
    if out.is_relative_to(ROOT):
        raise ValueError(f"{out} is inside the repository; the video pack must stay out of git")
    files = pack_files(outputs)
    if not files:
        raise ValueError(f"no video_*.mp4 under {outputs}")
    out.parent.mkdir(parents=True, exist_ok=True)
    index = io.StringIO()
    writer = csv.writer(index)
    writer.writerow(["file", "bytes", "sha256"])
    with zipfile.ZipFile(out, "w") as zf:
        for path, name in files:
            method = zipfile.ZIP_STORED if path.suffix == ".mp4" else zipfile.ZIP_DEFLATED
            zf.write(path, name, compress_type=method)
            writer.writerow([name, path.stat().st_size, sha256(path)])
        zf.writestr("pack_index.csv", index.getvalue(), compress_type=zipfile.ZIP_DEFLATED)
    videos = sum(1 for p, _ in files if p.suffix == ".mp4")
    return {"zip": str(out), "files": len(files) + 1, "videos": videos, "bytes": out.stat().st_size}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Zip all test pack videos + their json/srt outside the repo.")
    parser.add_argument("--outputs", type=Path, default=OUTPUTS)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    try:
        info = make_pack(args.outputs, args.out)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    print(f"{info['zip']}: {info['videos']} videos, {info['files']} files, {info['bytes'] / 1e6:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
