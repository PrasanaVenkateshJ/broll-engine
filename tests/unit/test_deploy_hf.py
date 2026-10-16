"""The Hugging Face Space folder + tools/deploy_hf.py: only deploy/hf-space/ is uploaded, no secret is
ever in it, and the Dockerfile / start.sh keep the rules the live demo depends on."""
import re
from pathlib import Path

import tools.deploy_hf as deploy

ROOT = Path(__file__).resolve().parents[2]
SPACE = ROOT / "deploy" / "hf-space"
DOCKERFILE = (SPACE / "Dockerfile").read_text(encoding="utf-8")
START = (SPACE / "start.sh").read_text(encoding="utf-8")


def test_only_the_space_folder_is_uploaded_with_lf_line_endings():
    files = deploy.space_files()
    assert set(files) == {"README.md", "Dockerfile", "start.sh", "preload_models.py", "REVISION"}
    for name, data in files.items():
        assert b"\r\n" not in data, name
    assert re.match(rb"^\S+ \d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\n$", files["REVISION"])  # commit + upload time


def test_space_readme_front_matter():
    front = (SPACE / "README.md").read_text(encoding="utf-8").split("---")[1]
    fields = dict(line.split(":", 1) for line in front.strip().splitlines())
    fields = {k.strip(): v.strip() for k, v in fields.items()}
    assert fields["title"] == "B-roll Studio" and fields["sdk"] == "docker" and fields["app_port"] == "7860"
    assert 0 < len(fields["short_description"]) <= 60  # Hugging Face's limit
    assert "@" not in (SPACE / "README.md").read_text(encoding="utf-8")  # no e-mail / personal info


def test_no_secret_values_in_the_space_folder():
    for path in SPACE.iterdir():
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"(ghp_|github_pat_|hf_[A-Za-z0-9]{20}|gsk_)[A-Za-z0-9_]{10,}", text), path.name
        assert not re.search(r"\b(API_TOKENS|FILE_LINK_SECRET|CALLBACK_SECRET|GROQ_API_KEY|PIXABAY_API_KEY)=[^\s$\"]",
                             text), path.name


def test_github_token_is_a_build_secret_and_never_stored():
    assert "RUN --mount=type=secret,id=GH_TOKEN,mode=0444,required=true" in DOCKERFILE
    assert not re.search(r"^(ARG|ENV)\s+GH_TOKEN", DOCKERFILE, re.M)  # never a build arg / env var (they persist)
    assert "@github.com" not in DOCKERFILE  # token never inside the clone URL (it would be saved in .git/config)
    assert "rm -rf /home/user/app/.git" in DOCKERFILE
    assert "unset GH_TOKEN" in START  # Spaces also pass secrets to the running app: drop it there


def test_dockerfile_rules():
    assert DOCKERFILE.count("FROM ") == 2 and "FROM python:3.11-slim" in DOCKERFILE
    compose_minio = re.search(r"image: (pgsty/minio:\S+)", (ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    assert f"FROM {compose_minio.group(1)} AS minio" in DOCKERFILE  # same MinIO as docker compose
    assert "COPY --from=minio /usr/bin/minio" in DOCKERFILE
    assert "useradd -m -u 1000 user" in DOCKERFILE and "\nUSER user" in DOCKERFILE
    req = (ROOT / "requirements-ml.txt").read_text(encoding="utf-8")
    for pin in re.findall(r"torch(?:vision)?==\S+", DOCKERFILE):
        assert pin in req, f"{pin} differs from requirements-ml.txt"
    for package in ("ffmpeg", "espeak-ng", "fonts-dejavu-core", "git", "curl"):
        assert package in DOCKERFILE
    assert "RUN python /home/user/preload_models.py" in DOCKERFILE


def test_start_script_settings():
    for line in ("export STORAGE_BACKEND=s3", "export ALLOW_PRIVATE_URLS=false", "--port 7860",
                 'WORKER_CONCURRENCY="${WORKER_CONCURRENCY:-1}"', 'FOOTAGE_WORKERS="${FOOTAGE_WORKERS:-2}"',
                 'TTS_PROVIDER="${TTS_PROVIDER:-kokoro}"', 'TRANSCRIBE_PROVIDER="${TRANSCRIBE_PROVIDER:-whisper}"',
                 'UNDERSTAND_PROVIDER="${UNDERSTAND_PROVIDER:-groq}"', 'RANK_SCORER="${RANK_SCORER:-clip}"',
                 "pixabay_video,pixabay_image_motion,wikimedia,generated", "READY"):
        assert line in START, line
    assert "for name in API_TOKENS FILE_LINK_SECRET CALLBACK_SECRET" in START  # auth ON: refuse to start without
    assert "--address 127.0.0.1:9000" in START  # MinIO only reachable inside the container
    assert "@" not in re.search(r"WIKIMEDIA_USER_AGENT:-([^}]*)", START).group(1)  # no e-mail
