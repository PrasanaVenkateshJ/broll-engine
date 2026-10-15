"""Intake: real file type via ffprobe, limits, conversion, downloads, empty scripts."""
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.config import get_settings
from app.errors import AppError
from app.pipeline import intake, media
from tests.media_helpers import make_tone


def error_code(excinfo) -> str:
    return excinfo.value.code.value


# ---------- Text ----------

@pytest.mark.parametrize("text", ["", "   ", "\n\t  \n", None])
def test_empty_or_whitespace_script_is_empty_script(text):
    with pytest.raises(AppError) as e:
        intake.check_script(text)
    assert error_code(e) == "EMPTY_SCRIPT"


def test_scene_list_with_blank_text_is_empty_script():
    with pytest.raises(AppError) as e:
        intake.check_scene_texts([{"text": "Fine."}, {"text": "   "}])
    assert error_code(e) == "EMPTY_SCRIPT"
    assert e.value.details == {"scene_indexes": [1]}


# ---------- Real file type ----------

@pytest.mark.parametrize("suffix,kind", [(".wav", "wav"), (".mp3", "mp3"), (".m4a", "m4a")])
def test_allowed_types_are_detected_and_converted_to_48k_mono(tmp_path, suffix, kind):
    src = make_tone(tmp_path / f"in{suffix}", seconds=2.0)
    info = intake.prepare_audio(src, tmp_path / "out.wav")
    assert info.source_kind == kind
    stream = media.probe(info.path)["streams"][0]
    assert stream["codec_name"] == "pcm_s16le"
    assert int(stream["sample_rate"]) == 48000
    assert stream["channels"] == 1
    assert abs(info.duration - 2.0) < 0.1


def test_type_comes_from_the_content_not_the_extension(tmp_path):
    wav_named_mp3 = make_tone(tmp_path / "x.wav").rename(tmp_path / "really_a_wav.mp3")
    assert intake.check_audio_file(wav_named_mp3).kind == "wav"


def test_text_file_renamed_to_mp3_is_unsupported(tmp_path):
    fake = tmp_path / "notes.mp3"
    fake.write_text("This is just a text file pretending to be audio.\n" * 20)
    with pytest.raises(AppError) as e:
        intake.check_audio_file(fake)
    assert error_code(e) == "UNSUPPORTED_FILE_TYPE"
    assert e.value.status_code == 415


def test_real_audio_of_another_type_is_unsupported(tmp_path):
    flac = make_tone(tmp_path / "voice.flac")
    with pytest.raises(AppError) as e:
        intake.check_audio_file(flac)
    assert error_code(e) == "UNSUPPORTED_FILE_TYPE"


def test_broken_audio_is_corrupted_media(tmp_path):
    broken = tmp_path / "broken.wav"
    broken.write_bytes(b"RIFF\x24\x08\x00\x00WAVE" + b"\x13\x37" * 500)  # wav header start, then garbage
    with pytest.raises(AppError) as e:
        intake.check_audio_file(broken)
    assert error_code(e) == "CORRUPTED_MEDIA"


def test_too_long_audio_is_file_too_long(tmp_path, monkeypatch):
    monkeypatch.setenv("MAX_AUDIO_SECONDS", "3")
    get_settings.cache_clear()
    src = make_tone(tmp_path / "long.mp3", seconds=5.0)
    with pytest.raises(AppError) as e:
        intake.check_audio_file(src)
    assert error_code(e) == "FILE_TOO_LONG"
    assert e.value.details["max_seconds"] == 3


# ---------- Download ----------

class FileServer:
    """Tiny local web server. routes: path -> (status, body bytes, send Content-Length?, delay seconds)."""

    def __init__(self, routes):
        server_self = self
        self.routes = routes

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                status, body, with_length, delay = server_self.routes.get(self.path, (404, b"missing", True, 0))
                self.send_response(status)
                if with_length:
                    self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                for i in range(0, len(body), 64 * 1024):
                    if delay:
                        time.sleep(delay)
                    self.wfile.write(body[i:i + 64 * 1024])

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


@pytest.fixture
def file_server(tmp_path):
    tone = make_tone(tmp_path / "tone.mp3").read_bytes()
    big = b"\0" * (6 * 1024 * 1024)  # MAX_DOWNLOAD_MB is 5 in tests
    server = FileServer({
        "/ok.mp3": (200, tone, True, 0),
        "/big.mp3": (200, big, True, 0),
        "/big-no-length.mp3": (200, big, False, 0),
        "/error.mp3": (500, b"oops", True, 0),
        "/empty.mp3": (200, b"", True, 0),
    })
    yield server
    server.close()


def test_download_ok(tmp_path, file_server):
    dest = intake.download_audio(f"{file_server.base}/ok.mp3", tmp_path / "dl")
    assert intake.check_audio_file(dest).kind == "mp3"


@pytest.mark.parametrize("path", ["/big.mp3", "/big-no-length.mp3"])
def test_download_too_large(tmp_path, file_server, path):
    dest = tmp_path / "dl"
    with pytest.raises(AppError) as e:
        intake.download_audio(f"{file_server.base}{path}", dest)
    assert error_code(e) == "FILE_TOO_LARGE"
    assert not dest.exists()  # half-downloaded file is removed


@pytest.mark.parametrize("path", ["/missing.mp3", "/error.mp3", "/empty.mp3"])
def test_download_http_problems(tmp_path, file_server, path):
    with pytest.raises(AppError) as e:
        intake.download_audio(f"{file_server.base}{path}", tmp_path / "dl")
    assert error_code(e) == "DOWNLOAD_FAILED"


def test_download_unreachable_host(tmp_path):
    with pytest.raises(AppError) as e:
        intake.download_audio("http://127.0.0.1:9/nothing.mp3", tmp_path / "dl")  # port 9: nobody listens
    assert error_code(e) == "DOWNLOAD_FAILED"
    assert e.value.retryable is True


def test_download_too_slow(tmp_path, file_server, monkeypatch):
    monkeypatch.setenv("DOWNLOAD_TIMEOUT_SECONDS", "0.5")
    get_settings.cache_clear()
    file_server.routes["/slow.mp3"] = (200, b"\0" * (64 * 1024 * 20), True, 0.1)  # 20 chunks x 0.1 s = 2 s
    with pytest.raises(AppError) as e:
        intake.download_audio(f"{file_server.base}/slow.mp3", tmp_path / "dl")
    assert error_code(e) == "DOWNLOAD_FAILED"
