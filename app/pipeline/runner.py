"""The whole pipeline for one job, stage by stage. The worker calls run_pipeline().

intake -> (tts) -> transcribe -> scenes + captions -> understand + safety -> footage
       -> render (segments + QA, then one video per ratio) -> upload -> save the swap cache

The swap cache (jobs/<job_id>/cache/ in storage) keeps the narration, every rendered segment and
state.json (scenes, captions, understanding, chosen clips). A later clip swap (swap.py) reuses
all of it and re-renders only ONE scene.

Each stage updates the job's progress (0-100) and stage name, and its time is recorded
for manifest.json. Everything happens in WORK_DIR/<job_id>, which is deleted at the end.
"""
import json
import logging
import shutil
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.errors import AppError, ErrorCode
from app.jobs import add_warning, set_progress
from app.pipeline import intake, media, qa
from app.pipeline.outputs import build_cues, build_manifest, build_timeline, write_json, write_srt
from app.pipeline.providers import get_provider, provider_status
from app.pipeline.rank import SelectionContext, _pool_map, get_scorer, select_clip, select_clips
from app.pipeline.render import (RESOLUTIONS, analyse_source, concat_segments, frame_counts, orientation_of,
                                 ratio_slug, render_failed, render_segment, save_thumbnail, subtitles_filter,
                                 thumbnail_filename, video_filename)
from app.pipeline.safety import plan_search
from app.pipeline.scenes import attach_search_text, scenes_from_list, split_into_scenes
from app.pipeline.transcribe import TranslateError, check_sound, check_speech, get_transcriber
from app.pipeline.tts import get_tts, synthesize_cached
from app.pipeline.understand import translate_scenes, understand_scenes
from app.pipeline.vlm import get_judge
from app.storage import cache_key, get_storage, output_key

log = logging.getLogger("app.pipeline.runner")

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
        if message in self.warnings:  # e.g. "provider X failed" for every scene: say it once
            return
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
    result, from_cache = synthesize_cached(tts, text, voice, run.work / "tts.wav")
    run.models["tts"] = {**tts.info(), "voice": voice, "from_cache": from_cache}
    return intake.prepare_audio(result.path, run.work / "narration.wav")


def _narration_from_scene_list(run: Run, items: list[dict], voice: str) -> tuple[intake.AudioInfo, list[float]]:
    """Speak each scene on its own, add a short pause, then join them. Returns the length of each part."""
    s = get_settings()
    tts = get_tts()
    parts, durations, hits = [], [], 0
    for i, item in enumerate(items):
        result, from_cache = synthesize_cached(tts, item["text"].strip(), voice, run.work / f"tts_{i:03d}.wav")
        raw, hits = result.path, hits + from_cache
        part = intake.convert_to_wav(raw, run.work / f"part_{i:03d}.wav", pad_seconds=s.SCENE_PAUSE_SECONDS)
        parts.append(part)
        durations.append(media.duration_of(part))
    run.models["tts"] = {**tts.info(), "voice": voice, "from_cache": f"{hits} of {len(items)} scenes"}
    (run.work / "parts.txt").write_text("".join(f"file '{p.name}'\n" for p in parts), encoding="utf-8")
    try:  # all parts are the same WAV format, so they can be joined without re-encoding
        media.ffmpeg("-f", "concat", "-safe", "0", "-i", "parts.txt", "-c", "copy", "joined.wav", cwd=run.work)
    except media.MediaError:
        raise AppError(ErrorCode.INTERNAL_ERROR, "Could not join the scene narrations", 500, retryable=True) from None
    found = intake.check_audio_file(run.work / "joined.wav")  # also enforces MAX_AUDIO_SECONDS
    return intake.AudioInfo(path=run.work / "joined.wav", duration=found.duration, source_kind="wav",
                            source_codec=found.codec), durations


def run_pipeline(job_id: str, request: dict[str, Any], input_type: str, input_key: str | None = None,
                 previous_timeline: dict | None = None) -> list[str]:
    """Run every stage. Returns the output file names saved in storage. Raises AppError on failure.

    previous_timeline: an earlier timeline.json of this job (re-render / clip swap); its stored scene
    understanding is reused when it still matches, so the LLM is not asked again.
    """
    s = get_settings()
    work = Path(s.WORK_DIR) / job_id
    work.mkdir(parents=True, exist_ok=True)
    run = Run(job_id, work)
    voice = request.get("voice") or "en-US-female"
    started = time.perf_counter()
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
            # A TTS voice tells us the language ("hi-IN-female" -> "hi"); an upload is auto-detected.
            language_hint = voice.split("-")[0].lower() if input_type in ("script", "scenes") else None
            transcript = transcriber.transcribe(audio.path, audio.duration, hints, language_hint)
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

        # 4. What should each scene show? (meaning + safety rules)
        with run.stage("understand", 48):
            if transcript.language != "en" and s.TRANSLATE_FOR_SEARCH:
                _english_for_search(run, transcriber, audio.path, transcript.language, scenes)
            full_text = text if input_type == "script" else " ".join(sc.text for sc in scenes)
            meanings, understand_info, understand_warnings = understand_scenes(scenes, full_text,
                                                                               request.get("style"),
                                                                               previous_timeline)
            run.models["understand"] = understand_info
            for message in understand_warnings:
                run.warn(message)
            plans = [plan_search(m, sc.text, request.get("avoid")) for m, sc in zip(meanings, scenes)]
            for plan in plans:
                for message in plan.warnings:
                    run.warn(message)

        # 5. Find one clip per scene (search, score, select, download, verify).
        ratios = request.get("aspect_ratios") or ["16:9"]
        ctx = SelectionContext(work=work, orientation=orientation_of(ratios[0]), warn=run.warn,
                               scorer=get_scorer(), judge=get_judge())
        with run.stage("footage", 52):
            # Pass 1 (parallel) searches + scores every scene; pass 2 picks one clip per scene, in order.
            selections, footage_facts = select_clips(
                scenes, meanings, plans, ctx,
                on_scene_done=lambda i: set_progress(job_id, 60 + round(10 * (i + 1) / len(scenes)), "footage"))
            vlm_facts = footage_facts.pop("vlm", {})
            run.models["ranker"] = {**ctx.scorer.info(), **footage_facts}
            if vlm_facts.get("judge"):
                run.models["vlm_judge"] = {**vlm_facts.pop("judge"), **vlm_facts}

        # 6. Render every scene segment (with QA), then one video per aspect ratio.
        with run.stage("render", 70):
            segments = _render_segments(run, scenes, cues, ratios, selections, meanings, plans, ctx,
                                        burn_captions=request.get("captions", True),
                                        language=transcript.language)
            videos, qa_report = [], {}
            for ratio in ratios:
                videos.append(_join_video(run, scenes, segments, ratio, audio))
                qa_report[ratio] = videos[-1].pop("qa")
            thumbnails = make_thumbnails(work, scenes, segments, ratios)

        # 7. timeline.json + manifest.json, then upload everything.
        with run.stage("upload", 92):
            write_json(build_timeline(job_id, input_type, audio.duration, transcript.language, scenes, meanings,
                                      plans, selections, segments, videos, thumbnails), work / "timeline.json")
            write_json(_manifest(run, request, input_type, audio, sound, transcript, selections, videos,
                                 qa_report), work / "manifest.json")
            # Stopwatch for the swap comparison: job start -> every output file written (upload excluded,
            # the same point swap.py measures at).
            full_seconds = round(time.perf_counter() - started, 3)
            files = [v["file"] for v in videos] + ["captions.srt", "timeline.json", "manifest.json"]
            files += [name for name in thumbnails.values() if name]
            storage = get_storage()
            for name in files:
                storage.put_file(output_key(job_id, name), work / name)
        with run.stage("cache", 97):
            try:  # the videos are already saved: a cache problem only means "no clip swap later"
                save_swap_cache(job_id, work, request, input_type, audio.duration, transcript.language, scenes, cues,
                                meanings, plans, selections, segments, ctx.used, run.timings, full_seconds)
            except Exception as e:
                run.warn(f"Could not save the clip-swap cache ({type(e).__name__}); swapping a clip will not work "
                         "for this job")
        return files
    finally:
        if not s.KEEP_WORK_FILES:
            shutil.rmtree(work, ignore_errors=True)


def save_swap_cache(job_id, work, request, input_type, duration, language, scenes, cues, meanings, plans, selections,
                    segments, used, timings, full_seconds) -> None:
    """Upload what a later clip swap needs: narration, every segment file, and state.json."""
    storage = get_storage()
    storage.put_file(cache_key(job_id, "narration.wav"), work / "narration.wav")
    for scene_segments in segments.values():
        for ratio, facts in scene_segments.items():
            folder = f"render_{ratio_slug(ratio)}"
            storage.put_file(cache_key(job_id, f"{folder}/{facts['file']}"), work / folder / facts["file"])
    state = {
        "version": 1, "root_job_id": job_id, "latest_job_id": job_id, "input_type": input_type,
        "aspect_ratios": request.get("aspect_ratios") or ["16:9"], "captions": request.get("captions", True),
        "duration": duration, "language": language,
        "scenes": [sc.to_dict() for sc in scenes],
        "cues": [{"start": c.start, "end": c.end, "lines": c.lines, "scene_index": c.scene_index} for c in cues],
        "meanings": [m.to_dict() for m in meanings],
        "plans": [p.to_dict() for p in plans],
        "selections": [sel.to_state() for sel in selections],
        "segments": {str(k): v for k, v in segments.items()},
        "used": sorted(used),  # every clip ever chosen (or rejected after download) in this video
        "full_render": {"seconds": full_seconds, "timings_seconds": dict(timings)},
        "swaps": [],
    }
    storage.put_bytes(cache_key(job_id, "state.json"), json.dumps(state, ensure_ascii=False).encode("utf-8"))


def _english_for_search(run: Run, transcriber, audio_path: Path, language: str, scenes) -> None:
    """Non-English narration: give each scene its English meaning (scene.search_text) for footage search.
    Each scene's OWN text is translated (Groq, per scene), so no words shift to a neighbouring scene. Scenes
    Groq does not answer get Whisper's translation of the whole audio, split by time (the old way).
    Captions keep the spoken words. If both fail, search uses the original text + a warning."""
    started = time.perf_counter()
    english, groq_facts = translate_scenes(scenes, language)
    for sc in scenes:
        sc.search_text = english.get(sc.index)
    missing = [sc for sc in scenes if not sc.search_text]
    pieces = 0
    if missing:
        try:
            translation = transcriber.translate(audio_path, language)
        except TranslateError as e:
            run.warn(f"English translation for footage search failed ({e}); searching with the {language} text")
            translation = []
        pieces = len(translation)
        attach_search_text(scenes, translation)  # split by time over ALL scenes, then Groq's answers win
        for sc in scenes:
            if sc.index in english:
                sc.search_text = english[sc.index]
    run.models.setdefault("transcribe", {})["translate_for_search"] = {
        "method": "each scene's own text translated by Groq; Whisper whole-audio translation only for the rest",
        "from_language": language, "to_language": "en", "groq": groq_facts,
        "scenes_by_groq": len(english), "scenes_by_whisper": sum(1 for sc in missing if sc.search_text),
        "whisper_pieces": pieces, "scenes_with_english": sum(1 for sc in scenes if sc.search_text),
        "seconds": round(time.perf_counter() - started, 3),
        "used_for": "footage search queries only; captions stay in the spoken language"}


def _render_segments(run: Run, scenes, cues, ratios, selections, meanings, plans, ctx, burn_captions,
                     retry_with_next: bool = True, language: str | None = None) -> dict:
    """Render each scene for every ratio and check it. A segment that fails QA gets another clip
    (up to QA_RETRIES times); if it still fails, we keep it and add a warning.
    retry_with_next=False (a clip the user chose in a swap): never replace it, only warn.
    language = the caption language (picks the caption font, e.g. Noto Sans Tamil for "ta").

    Returns {scene index: {ratio: facts about the segment}} and updates `selections` in place.
    """
    s = get_settings()
    counts = frame_counts(scenes, s.RENDER_FPS)
    folders = {}
    for ratio in ratios:
        if ratio not in RESOLUTIONS:
            raise AppError(ErrorCode.UNSUPPORTED_ASPECT_RATIO, f"Unsupported aspect ratio {ratio}", 422)
        folders[ratio] = run.work / f"render_{ratio_slug(ratio)}"
        folders[ratio].mkdir(parents=True, exist_ok=True)

    for folder in folders.values():
        subtitles_filter("fonts.ass", folder)  # copy the fonts once, before segments render side by side

    def attempt(i: int) -> tuple[dict, list[str]]:
        """Render scene i for every ratio + QA. Returns ({ratio: facts}, problems)."""
        selection, scene, frames = selections[i], scenes[i], counts[i]
        rendered, problems = {}, []
        try:
            info = analyse_source(selection, frames / s.RENDER_FPS, plans[i].queries, ctx.scorer)
            for ratio in ratios:
                path, facts = render_segment(info, scene, ratio, cues, folders[ratio], frames, burn_captions,
                                             language)
                found = qa.check_segment(path, frames)
                rendered[ratio] = {**facts, "qa_problems": found}
                problems += [f"{ratio}: {p}" for p in found]
        except media.MediaError as e:
            if selection.candidate.generated:
                raise render_failed(ratios[0], e) from None
            problems.append(f"could not be rendered ({e})")
        return rendered, problems

    # First try of every scene: RENDER_WORKERS scenes at the same time (each segment is its own ffmpeg).
    # A scene that fails QA is retried afterwards, one at a time IN SCENE ORDER, so a replacement clip is
    # chosen exactly as before (the no-repeat rule does not depend on thread timing).
    first = _pool_map(attempt, list(range(len(scenes))), max(1, s.RENDER_WORKERS))
    segments: dict[int, dict] = {}
    for i, scene in enumerate(scenes):
        rendered, problems = first[i]
        retries = 0
        while problems:
            selection = selections[i]
            if selection.candidate.generated or retries >= s.QA_RETRIES or not retry_with_next:
                if len(rendered) < len(ratios):  # some ratio has no segment at all
                    raise render_failed(ratios[0], media.MediaError("; ".join(problems)))
                run.warn(f"Scene {scene.index}: quality check found {'; '.join(problems)} "
                         f"(kept clip {selection.candidate.uid})")
                break
            retries += 1
            run.warn(f"Scene {scene.index}: clip {selection.candidate.uid} failed the quality check "
                     f"({problems[0]}); trying another clip")
            selections[i] = select_clip(scene, meanings[i], plans[i], ctx)  # the failed clip is in ctx.used
            rendered, problems = attempt(i)
        segments[scene.index] = rendered
        for ratio, facts in rendered.items():  # e.g. "no clear subject found ... centre crop" (render.plan_framing)
            if (warning := (facts.get("crop") or {}).get("warning")):
                run.warn(f"Scene {scene.index} ({ratio}): {warning}")
        if retry_with_next:  # a swap reports its own progress
            set_progress(run.job_id, 70 + round(20 * (i + 1) / len(scenes)), "render")
    return segments


def make_thumbnails(work: Path, scenes, segments: dict, ratios: list[str], only: set[int] | None = None) -> dict:
    """One small 16:9 JPEG poster frame per scene (thumb_000.jpg ...), for the Studio's scene cards.

    Taken from the scene's 16:9 segment (or the first ratio's, padded to 16:9). only = just these scene
    indexes (a clip swap). Returns {scene index: file name, or None if ffmpeg failed}. A failed thumbnail
    never fails the job: the videos are fine, the card just shows no picture.
    """
    ratio = "16:9" if "16:9" in ratios else ratios[0]
    folder = work / f"render_{ratio_slug(ratio)}"
    fps = get_settings().RENDER_FPS
    out = {}
    for sc in scenes:
        if only is not None and sc.index not in only:
            continue
        facts = segments[sc.index][ratio]
        name = thumbnail_filename(sc.index)
        try:
            save_thumbnail(folder / facts["file"], work / name, facts.get("frames", fps) / fps)
            out[sc.index] = name
        except media.MediaError as e:
            log.warning("thumbnail for scene %d failed: %s", sc.index, e)
            out[sc.index] = None
    return out


def _join_video(run: Run, scenes, segments: dict, ratio: str, audio) -> dict:
    """Concat this ratio's segments + narration, then check the final file."""
    width, height = RESOLUTIONS[ratio]
    folder = run.work / f"render_{ratio_slug(ratio)}"
    out_path = run.work / video_filename(ratio)
    try:
        concat_segments([folder / segments[sc.index][ratio]["file"] for sc in scenes], audio.path, folder, out_path)
    except media.MediaError as e:
        raise render_failed(ratio, e) from None
    problems, facts = qa.check_video(out_path, audio.duration, width, height)
    for problem in problems:
        run.warn(f"{ratio} video: {problem}")
    return {"aspect_ratio": ratio, "file": out_path.name, "width": width, "height": height,
            "fps": get_settings().RENDER_FPS, "duration": facts["video_seconds"],
            "qa": {**facts, "problems": problems}}


def _manifest(run: Run, request, input_type, audio, sound, transcript, selections, videos, qa_report) -> dict:
    s = get_settings()
    providers = {}
    for name in s.provider_order:
        status = provider_status(name)
        info = get_provider(name).info() if status != "unknown provider name" else {"name": name}
        providers[name] = {**info, "status": status or "on",
                           "scenes_used": sum(sel.candidate.provider == name for sel in selections)}
    return build_manifest(
        job_id=run.job_id, created_at=datetime.now(timezone.utc).isoformat(),
        app={"name": s.APP_NAME, "version": s.APP_VERSION},
        input_facts={"type": input_type, "source_kind": audio.source_kind, "source_codec": audio.source_codec,
                     "duration": round(audio.duration, 3),
                     "voice": request.get("voice") if "tts" in run.models else None,
                     "style": request.get("style"), "avoid": request.get("avoid") or []},
        models={**run.models, "ffmpeg": media.ffmpeg_version()}, providers=providers,
        speech_check={"max_volume_db": sound.max_volume_db, "sound_seconds": sound.sound_seconds,
                      "words": len(transcript.words), "no_speech_prob": transcript.no_speech_prob,
                      "language": transcript.language, "language_probability": transcript.language_probability},
        timings=run.timings, warnings=run.warnings, selections=selections, videos=videos, qa=qa_report)
