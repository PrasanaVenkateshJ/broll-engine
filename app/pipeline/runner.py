"""The whole pipeline for one job, stage by stage. The worker calls run_pipeline().

intake -> (tts) -> transcribe -> scenes -> captions -> render (each ratio) -> upload

Each stage updates the job's progress (0-100) and stage name, and its time is recorded
for manifest.json. Everything happens in WORK_DIR/<job_id>, which is deleted at the end.
"""
import shutil
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.errors import AppError, ErrorCode
from app.jobs import add_warning, set_progress
from app.pipeline import intake, media
from app.pipeline.outputs import build_cues, write_json, write_srt
from app.pipeline.render import placeholder_color, render_video, video_filename
from app.pipeline.scenes import scenes_from_list, split_into_scenes
from app.pipeline.transcribe import check_sound, check_speech, get_transcriber
from app.pipeline.tts import get_tts
from app.storage import get_storage, output_key


class Run:
    """What we learn while running one job (for timeline.json and manifest.json)."""

    def __init__(self, job_id: str, work: Path):
        self.job_id = job_id
        self.work = work
        self.timings: dict[str, float] = {}
        self.models: dict[str, dict] = {}
        self.warnings: list[str] = []

    @contextmanager
    def stage(self, name: str, progress: int):
        set_progress(self.job_id, progress, name)
        start = time.perf_counter()
        try:
            yield
        finally:
            self.timings[name] = round(self.timings.get(name, 0.0) + time.perf_counter() - start, 3)

    def warn(self, message: str) -> None:
        self.warnings.append(message)
        add_warning(self.job_id, message)


def _narration_from_audio(run: Run, request: dict, input_type: str, input_key: str | None) -> intake.AudioInfo:
    raw = run.work / "input_audio"
    if input_type == "audio_url":
        intake.download_audio(request["audio_url"], raw)
    else:  # upload: the API saved the file in storage before queuing the job
        if not input_key:
            raise AppError(ErrorCode.INVALID_INPUT, "The job has no uploaded file", 422)
        get_storage().get_file(input_key, raw)
    return intake.prepare_audio(raw, run.work / "narration.wav")


def _narration_from_script(run: Run, text: str, voice: str) -> intake.AudioInfo:
    tts = get_tts()
    run.models["tts"] = {**tts.info(), "voice": voice}
    result = tts.synthesize(text, voice, run.work / "tts.wav")
    return intake.prepare_audio(result.path, run.work / "narration.wav")


def _narration_from_scene_list(run: Run, items: list[dict], voice: str) -> tuple[intake.AudioInfo, list[float]]:
    """Speak each scene on its own, add a short pause, then join them. Returns the length of each part."""
    s = get_settings()
    tts = get_tts()
    run.models["tts"] = {**tts.info(), "voice": voice}
    parts, durations = [], []
    for i, item in enumerate(items):
        raw = tts.synthesize(item["text"].strip(), voice, run.work / f"tts_{i:03d}.wav").path
        part = intake.convert_to_wav(raw, run.work / f"part_{i:03d}.wav", pad_seconds=s.SCENE_PAUSE_SECONDS)
        parts.append(part)
        durations.append(media.duration_of(part))
    (run.work / "parts.txt").write_text("".join(f"file '{p.name}'\n" for p in parts), encoding="utf-8")
    try:  # all parts are the same WAV format, so they can be joined without re-encoding
        media.ffmpeg("-f", "concat", "-safe", "0", "-i", "parts.txt", "-c", "copy", "joined.wav", cwd=run.work)
    except media.MediaError:
        raise AppError(ErrorCode.INTERNAL_ERROR, "Could not join the scene narrations", 500, retryable=True) from None
    found = intake.check_audio_file(run.work / "joined.wav")  # also enforces MAX_AUDIO_SECONDS
    return intake.AudioInfo(path=run.work / "joined.wav", duration=found.duration, source_kind="wav",
                            source_codec=found.codec), durations


def run_pipeline(job_id: str, request: dict[str, Any], input_type: str, input_key: str | None = None) -> list[str]:
    """Run every stage. Returns the output file names saved in storage. Raises AppError on failure."""
    s = get_settings()
    work = Path(s.WORK_DIR) / job_id
    work.mkdir(parents=True, exist_ok=True)
    run = Run(job_id, work)
    voice = request.get("voice") or "en-US-female"
    try:
        # 1. Get the narration as a 48 kHz mono WAV.
        item_durations = None
        with run.stage("intake", 5):
            if input_type == "script":
                text = intake.check_script(request.get("script"))
            elif input_type == "scenes":
                intake.check_scene_texts(request["scenes"])
            else:
                audio = _narration_from_audio(run, request, input_type, input_key)
        if input_type in ("script", "scenes"):
            with run.stage("tts", 10):
                if input_type == "script":
                    audio = _narration_from_script(run, text, voice)
                else:
                    audio, item_durations = _narration_from_scene_list(run, request["scenes"], voice)

        # 2. Is there speech? Then get every word with its time.
        with run.stage("transcribe", 25):
            sound = check_sound(audio.path, audio.duration)
            if input_type == "script":
                hints = [(text, 0.0, audio.duration)]
            elif input_type == "scenes":
                starts = [sum(item_durations[:i]) for i in range(len(item_durations))]
                hints = [(it["text"], st, st + d) for it, st, d in zip(request["scenes"], starts, item_durations)]
            else:
                hints = None
            transcriber = get_transcriber()
            transcript = transcriber.transcribe(audio.path, audio.duration, hints)
            run.models["transcribe"] = transcript.info
            check_speech(transcript, sound)

        # 3. Scenes + captions.
        with run.stage("scenes", 45):
            if input_type == "scenes":
                scenes = scenes_from_list(request["scenes"], item_durations, transcript.words, audio.duration)
                for sc in scenes:
                    if sc.duration > s.MAX_SCENE_SECONDS:
                        run.warn(f"Scene {sc.index} is {sc.duration:.1f} s long (over {s.MAX_SCENE_SECONDS:g} s); "
                                 "scene-list items are kept as given")
            else:
                scenes = split_into_scenes(transcript.words, audio.duration)
            cues = build_cues(scenes)
            write_srt(cues, work / "captions.srt")

        # 4. One video per aspect ratio.
        ratios = request.get("aspect_ratios") or ["16:9"]
        videos = []
        for i, ratio in enumerate(ratios):
            with run.stage("render", 50 + round(40 * i / len(ratios))):
                videos.append(render_video(scenes, cues, audio.path, ratio, work, work / video_filename(ratio),
                                           burn_captions=request.get("captions", True)))

        # 5. timeline.json + manifest.json, then upload everything.
        with run.stage("upload", 92):
            write_json(_timeline(job_id, input_type, audio, transcript, scenes, videos), work / "timeline.json")
            write_json(_manifest(run, request, input_type, audio, sound, transcript, videos), work / "manifest.json")
            files = [v["file"] for v in videos] + ["captions.srt", "timeline.json", "manifest.json"]
            storage = get_storage()
            for name in files:
                storage.put_file(output_key(job_id, name), work / name)
        return files
    finally:
        if not s.KEEP_WORK_FILES:
            shutil.rmtree(work, ignore_errors=True)


def _timeline(job_id, input_type, audio, transcript, scenes, videos) -> dict:
    return {
        "job_id": job_id,
        "input_type": input_type,
        "duration": round(audio.duration, 3),
        "language": transcript.language,
        "scenes": [{**sc.to_dict(), "clip": {
            "provider": "placeholder", "color": placeholder_color(sc.index), "generated": True,
            "ai_generated": False, "reason": "placeholder colour (footage search is not built yet)"}}
            for sc in scenes],
        "videos": videos,
    }


def _manifest(run: Run, request, input_type, audio, sound, transcript, videos) -> dict:
    s = get_settings()
    return {
        "job_id": run.job_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "app": {"name": s.APP_NAME, "version": s.APP_VERSION},
        "input": {"type": input_type, "source_kind": audio.source_kind, "source_codec": audio.source_codec,
                  "duration": round(audio.duration, 3), "voice": request.get("voice") if "tts" in run.models else None},
        "models": run.models,
        "settings": {k: getattr(s, k) for k in (
            "MIN_SCENE_SECONDS", "MAX_SCENE_SECONDS", "PAUSE_SECONDS", "RENDER_FPS", "VIDEO_PRESET", "VIDEO_CRF",
            "AUDIO_SAMPLE_RATE", "AUDIO_BITRATE", "CAPTION_MAX_CHARS_PER_LINE", "CAPTION_MAX_LINES")},
        "speech_check": {"max_volume_db": sound.max_volume_db, "sound_seconds": sound.sound_seconds,
                         "words": len(transcript.words), "no_speech_prob": transcript.no_speech_prob,
                         "language": transcript.language, "language_probability": transcript.language_probability},
        "timings_seconds": run.timings,
        "warnings": run.warnings,
        "ai_generated": {"narration": "tts" in run.models, "visuals": False},
        "outputs": videos,
    }
