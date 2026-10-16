"""Upload ONLY deploy/hf-space/ to a Hugging Face Docker Space (one commit). Nothing else of this repo
is sent: the Space's Dockerfile clones the app code from GitHub itself, with the GH_TOKEN build secret.

The Hugging Face token (a "write" token from huggingface.co/settings/tokens) comes from the HF_TOKEN
environment variable, or is asked for with a hidden prompt. It is only kept in memory: never written
to a file, never printed.

    python tools/deploy_hf.py --space <hf-username>/broll-studio
    python tools/deploy_hf.py --space <hf-username>/broll-studio --dry-run    (only list the files)

What is uploaded: every file in deploy/hf-space/ (line endings turned into LF, so start.sh runs on
Linux), with REVISION replaced by "<git commit> <upload time>". A changed REVISION makes the Space
re-clone the code instead of using an old cached copy. Files in the Space that are not in the folder
are deleted (except .gitattributes, which Hugging Face creates).
"""
import argparse
import getpass
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FOLDER = ROOT / "deploy" / "hf-space"
KEEP_IN_SPACE = {".gitattributes"}
TEXT_SUFFIXES = {".md", ".sh", ".py", ""}  # "" = Dockerfile, REVISION


def git(*args: str) -> str:
    try:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def space_files() -> dict[str, bytes]:
    """path in the Space -> file content. Text files get LF line endings."""
    files = {}
    for path in sorted(p for p in FOLDER.rglob("*") if p.is_file() and "__pycache__" not in p.parts):
        data = path.read_bytes()
        if path.suffix in TEXT_SUFFIXES:
            data = data.replace(b"\r\n", b"\n")
        files[path.relative_to(FOLDER).as_posix()] = data
    commit = git("rev-parse", "HEAD") or "unknown"
    files["REVISION"] = f"{commit} {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n".encode()
    return files


def warn_about_git_state(branch: str) -> None:
    """The Space builds from GitHub, not from this PC: say so if what is here is not on GitHub yet."""
    if git("status", "--porcelain"):
        print("WARNING: you have uncommitted changes. The Space builds from GitHub, so they are NOT deployed.")
    head = git("rev-parse", "HEAD")
    if head and head != git("rev-parse", f"origin/{branch}"):
        print(f"WARNING: your HEAD ({head[:8]}) is not origin/{branch} (as far as the last git fetch knows). "
              f"Push first, or the Space builds the older origin/{branch}.")


def get_token() -> str:
    token = os.environ.get("HF_TOKEN", "").strip()
    if token:
        print("using the Hugging Face token from HF_TOKEN")
        return token
    return getpass.getpass("Hugging Face WRITE token (input hidden): ").strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Upload deploy/hf-space/ to a Hugging Face Docker Space.")
    parser.add_argument("--space", default=os.environ.get("HF_SPACE_ID", ""),
                        help="Space id <hf-username>/<space-name> (default: $HF_SPACE_ID)")
    parser.add_argument("--branch", default="main", help="GitHub branch the Space builds (only for the warning)")
    parser.add_argument("--dry-run", action="store_true", help="list what would be uploaded, upload nothing")
    args = parser.parse_args(argv)
    if args.space.count("/") != 1:
        parser.error("--space must look like <hf-username>/broll-studio")

    files = space_files()
    print(f"files for {args.space}:")
    for name, data in files.items():
        print(f"  {name:22} {len(data):>7} bytes")
    warn_about_git_state(args.branch)
    if args.dry_run:
        return 0

    try:
        from huggingface_hub import CommitOperationAdd, CommitOperationDelete, HfApi
        from huggingface_hub.errors import HfHubHTTPError, RepositoryNotFoundError
    except ImportError:
        print("huggingface_hub is missing: .venv\\Scripts\\python -m pip install huggingface_hub", file=sys.stderr)
        return 2

    token = get_token()
    if not token:
        print("no token given", file=sys.stderr)
        return 2
    api = HfApi(token=token)
    try:
        existing = set(api.list_repo_files(args.space, repo_type="space"))
        operations = [CommitOperationAdd(path_in_repo=name, path_or_fileobj=data) for name, data in files.items()]
        operations += [CommitOperationDelete(path_in_repo=name)
                       for name in sorted(existing - set(files) - KEEP_IN_SPACE)]
        commit = api.create_commit(args.space, repo_type="space", operations=operations,
                                   commit_message=f"Deploy {files['REVISION'].decode().strip()}")
    except RepositoryNotFoundError:
        print(f"Space {args.space} not found (or this token cannot see it). Create it first.", file=sys.stderr)
        return 1
    except HfHubHTTPError as e:  # e.g. 401 wrong token, 403 read-only token
        status = e.response.status_code if e.response is not None else "?"
        print(f"upload failed: HTTP {status}. Is it a WRITE token for this account?", file=sys.stderr)
        return 1
    print(f"uploaded: {commit.commit_url}")
    print(f"build logs: https://huggingface.co/spaces/{args.space}  (open the 'Logs' button, 'Build' tab)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
