"""Clip swap: give ONE scene of a finished video a different clip, re-rendering only that scene.

Reused from the swap cache that the full job saved (runner.save_swap_cache):
- the narration WAV, the scenes with their word times, the captions,
- every scene's saved understanding + safety plan (the LLM is NOT asked again),
- every OTHER scene's rendered segment, for every aspect ratio.
Redone: choose the new clip -> render that scene's segment per ratio (+ QA) -> join all segments
again (stream copy, no re-encoding) + narration -> QA the videos -> upload -> new timeline.json
and manifest.json with a history of swaps and the swap time next to the full render time.

Three ways to choose the new clip (SwapRequest.choice):
- next_best:     the normal selection rule again, with every clip ever used in this video excluded,
- provider_clip: one clip by its id from a footage source (provider.lookup()),
- clip_url:      a file by URL. Pixabay addresses get the Pixabay licence, Wikimedia files are looked
                 up and must pass the CC0 / PD / CC BY filter, any other public address must be
                 marked user_supplied and carry its licence in the request.
The swap always applies to the LATEST version of the video (state.json is updated after each swap).
"""
import hashlib
import json
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote, urlsplit

from app.config import get_settings
from app.errors import AppError, ErrorCode
from app.jobs import set_progress
from app.pipeline import media, net
from app.pipeline.outputs import Cue, build_timeline, rights_summary, write_json
from app.pipeline.providers import Candidate, ClipNotUsable, ProviderError, get_provider, provider_status
from app.pipeline.rank import Selection, SelectionContext, frame_logo_check, get_scorer, select_clip, verify_media
from app.pipeline.render import orientation_of, ratio_slug
from app.pipeline.runner import Run, _join_video, _render_segments
from app.pipeline.safety import SearchPlan
from app.pipeline.scenes import Scene
from app.pipeline.transcribe import Word
from app.pipeline.understand import SceneMeaning
from app.schemas import SwapRequest
from app.storage import StorageNotFound, cache_key, get_storage, output_key

PIXABAY_HOSTS = {"pixabay.com", "cdn.pixabay.com"}
WIKIMEDIA_HOSTS = {"upload.wikimedia.org", "commons.wikimedia.org"}
LOOKUP_PROVIDERS = ("pixabay_video", "pixabay_image_motion", "wikimedia", "mock")
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp")


# ---------- Saved state ----------

def read_state(root_job_id: str) -> dict | None:
    """The swap cache's state.json of a job, or None if the job has none."""
    try:
        chunks, _ = get_storage().open_stream(cache_key(root_job_id, "state.json"))
    except StorageNotFound:
        return None
    return json.loads(b"".join(chunks).decode("utf-8"))


def url_source(url: str) -> str | None:
    """"pixabay" | "wikimedia" for addresses whose licence we can check ourselves, else None."""
    host = (urlsplit(url).hostname or "").lower()
    if host in PIXABAY_HOSTS:
        return "pixabay"
    if host in WIKIMEDIA_HOSTS:
        return "wikimedia"
    return None


def _uid(selection_state: dict) -> str:
    c = selection_state["candidate"]
    return f"{c['provider']}:{c['id']}"


def precheck(state: dict | None, scene_index: int, req: SwapRequest) -> None:
    """Errors the API can answer straight away (4xx) instead of queuing a swap that must fail."""
    if state is None:
        raise AppError(ErrorCode.JOB_NOT_READY, "This job has no clip-swap cache (it was made before clip swap "
                       "existed, or saving the cache failed); run the job again", 409)
    if not 0 <= scene_index < len(state["scenes"]):
        raise AppError(ErrorCode.SCENE_NOT_FOUND, f"Scene {scene_index} not found; the video has scenes "
                       f"0 to {len(state['scenes']) - 1}", 404, details={"scenes": len(state["scenes"])})
    in_video = {_uid(sel): i for i, sel in enumerate(state["selections"])}
    if req.choice == "provider_clip":
        if req.provider not in LOOKUP_PROVIDERS:
            raise AppError(ErrorCode.INVALID_INPUT, f"provider must be one of {list(LOOKUP_PROVIDERS)}", 422,
                           details={"provider": req.provider})
        if status := provider_status(req.provider):
            raise AppError(ErrorCode.PROVIDER_UNAVAILABLE, f"Footage source {req.provider} is off ({status})", 503,
                           details={"provider": req.provider})
        uid = f"{req.provider}:{req.clip_id}"
        if uid in in_video:
            raise _already_used(uid, in_video[uid])
    if req.choice == "clip_url":
        url = str(req.clip_url)
        if url_source(url) is None and not req.user_supplied:
            raise AppError(ErrorCode.INVALID_INPUT, "clip_url must be a Pixabay or Wikimedia address, or set "
                           "user_supplied: true with licence, licence_url, attribution and commercial_use", 422,
                           details={"allowed_hosts": sorted(PIXABAY_HOSTS | WIKIMEDIA_HOSTS)})
        for i, sel in enumerate(state["selections"]):
            if url in (sel["candidate"].get("download_url"), sel["candidate"].get("extra", {}).get("clip_url")):
                raise _already_used(url, i)


def _already_used(clip: str, scene: int) -> AppError:
    return AppError(ErrorCode.INVALID_INPUT, f"Clip {clip} is already used in this video (scene {scene}); "
                    "a clip id is never used twice", 422, details={"reason": "clip_already_used", "scene": scene})


def _load(state: dict) -> SimpleNamespace:
    scenes = [Scene(d["index"], d["start"], d["end"], d["text"], [Word(**w) for w in d["words"]], d.get("visual_hint"))
              for d in state["scenes"]]
    return SimpleNamespace(
        scenes=scenes,
        cues=[Cue(**c) for c in state["cues"]],
        meanings=[SceneMeaning.from_dict(i, m) for i, m in enumerate(state["meanings"])],
        plans=[SearchPlan(queries=p["queries"], exclude_terms=p["exclude_terms"], no_faces=p["no_faces"],
                          notes=p.get("notes") or []) for p in state["plans"]],
        selections=[Selection.from_state(i, d) for i, d in enumerate(state["selections"])],
        segments={int(k): v for k, v in state["segments"].items()},
    )


# ---------- Choosing the new clip ----------

def _check_chosen(cand: Candidate, path: Path, plan: SearchPlan, ctx: SelectionContext, scene_index: int,
                  how: str) -> Selection:
    """A clip the USER picked: it must be a real, readable file without a logo. Its relevance score is
    measured and recorded, but STOCK_MIN_SCORE is not applied (the user decided)."""
    s = get_settings()
    problem = verify_media(path, cand.kind)
    if problem:
        raise AppError(ErrorCode.CORRUPTED_MEDIA, f"The chosen clip cannot be used: {problem}", 422)
    rejected, frames = frame_logo_check(cand, path, plan.queries, ctx.scorer, ctx.work / "frames")
    if rejected and s.LOGO_CHECK:
        raise AppError(ErrorCode.INVALID_INPUT, "The chosen clip shows a logo / brand text (frame logo check)", 422,
                       details={"reason": "logo_check_failed", "frames": frames})
    score = round(sum(f["scene_score"] for f in frames) / len(frames), 4)
    logo = {"thumbnail": None, "frames": frames, "result": "ok"} if s.LOGO_CHECK else None
    reason = (f"swap: clip chosen by the user ({how}); relevance score {score:.3f} measured on "
              f"{len(frames)} frame(s) (STOCK_MIN_SCORE {s.STOCK_MIN_SCORE:.2f} is not applied to a user's choice)")
    if logo:
        reason += f"; frame logo check ok (max gap {max(f['gap'] for f in frames):+.3f})"
    return Selection(scene_index, cand, path, score, None, reason, "", logo)


def _download(provider_name: str | None, cand: Candidate, folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    try:
        if provider_name:
            return get_provider(provider_name).fetch(cand, folder)
        s = get_settings()
        suffix = Path(urlsplit(cand.download_url).path).suffix.lower() or ".mp4"
        return net.fetch_to_file(cand.download_url, folder / f"{cand.provider}_{cand.id}{suffix[:6]}",
                                 s.MAX_CLIP_DOWNLOAD_MB * net.MB, s.CLIP_DOWNLOAD_TIMEOUT_SECONDS)
    except net.DownloadError as e:
        code = ErrorCode.FILE_TOO_LARGE if e.reason == "too_large" else ErrorCode.DOWNLOAD_FAILED
        raise AppError(code, f"Downloading the chosen clip failed: {e}", 422, retryable=e.retryable,
                       details={"reason": e.reason}) from None


def _lookup(provider_name: str, clip_id: str) -> Candidate:
    if status := provider_status(provider_name):
        raise AppError(ErrorCode.PROVIDER_UNAVAILABLE, f"Footage source {provider_name} is off ({status})", 503)
    try:
        return get_provider(provider_name).lookup(clip_id)
    except ClipNotUsable as e:
        raise AppError(ErrorCode.INVALID_INPUT, f"{provider_name} clip {clip_id} cannot be used: {e}", 422) from None
    except ProviderError as e:
        raise AppError(ErrorCode.PROVIDER_UNAVAILABLE, f"Footage source {provider_name} failed: {e}", 503,
                       retryable=True) from None


def _candidate_from_url(req: SwapRequest) -> tuple[str | None, Candidate]:
    """(provider to download with or None, candidate) for a clip_url."""
    url = str(req.clip_url)
    path = urlsplit(url).path
    kind = "image" if path.lower().endswith(IMAGE_SUFFIXES) else "video"
    source = url_source(url)
    if source == "wikimedia":
        # .../commons/a/ab/Name.jpg  or a thumbnail .../thumb/a/ab/Name.jpg/500px-Name.jpg -> "Name.jpg"
        parts = [unquote(p) for p in path.split("/") if p]
        name = parts[parts.index("thumb") + 3] if "thumb" in parts and len(parts) > parts.index("thumb") + 3 \
            else parts[-1].removeprefix("File:")
        cand = _lookup("wikimedia", name)  # the licence filter decides, not the address
        cand.extra["clip_url"] = url
        return "wikimedia", cand
    digest = hashlib.sha1(url.encode()).hexdigest()[:12]
    if source == "pixabay":
        from app.pipeline.providers.pixabay import LICENCE_NAME, LICENCE_URL

        # cdn.pixabay.com/video/2024/01/02/123456-789_medium.mp4  /  .../photo/.../name-1234567_1280.jpg
        found = re.search(r"/video/\d{4}/\d{2}/\d{2}/(\d+)-", path) or re.search(r"-(\d+)_\d+\.\w+$", path)
        name = "pixabay_image_motion" if kind == "image" else "pixabay_video"
        return None, Candidate(provider=name, id=found.group(1) if found else f"url-{digest}", kind=kind,
                               download_url=url, licence_name=LICENCE_NAME, licence_url=LICENCE_URL,
                               attribution="Pixabay", page_url=url, extra={"clip_url": url})
    return None, Candidate(provider="user_supplied", id=f"url-{digest}", kind=kind, download_url=url,
                           licence_name=req.licence or "", licence_url=str(req.licence_url or ""),
                           attribution=req.attribution or "", page_url=url, commercial_use=True,
                           extra={"clip_url": url, "user_supplied": True, "licence_declared_by": "user"})


def choose_clip(req: SwapRequest, scene: Scene, meaning: SceneMeaning, plan: SearchPlan, ctx: SelectionContext,
                old: Selection, in_video: dict[str, int]) -> Selection:
    """in_video: uid -> scene index of every clip in the current video."""
    used_before = set(ctx.used)
    if req.choice == "next_best":
        sel = select_clip(scene, meaning, plan, ctx)
        if sel.candidate.uid in used_before:  # only the old fallback card was left
            raise AppError(ErrorCode.NO_ALTERNATIVE_CLIP, f"Scene {scene.index} has no other usable clip: every "
                           "candidate is already used in this video or fails the selection rule", 422,
                           details={"current_clip": old.candidate.uid})
        sel.reason = f"swap next_best (clip {old.candidate.uid} and every clip already used are excluded): {sel.reason}"
        return sel
    if req.choice == "provider_clip":
        provider_name, cand = req.provider, _lookup(req.provider, req.clip_id)
        how = f"provider {req.provider}, clip_id {req.clip_id}"
    else:
        provider_name, cand = _candidate_from_url(req)
        how = "clip_url" + (", user_supplied licence" if req.user_supplied else "")
    if cand.uid in in_video:
        raise _already_used(cand.uid, in_video[cand.uid])
    path = _download(provider_name, cand, ctx.work / "clips")
    sel = _check_chosen(cand, path, plan, ctx, scene.index, how)
    ctx.used.add(cand.uid)
    return sel


# ---------- The swap job ----------

def run_swap(job_id: str, request: dict) -> list[str]:
    """Run one swap job. Returns the output file names saved under this swap job. Raises AppError."""
    s = get_settings()
    root, index = request["root_job_id"], int(request["scene_index"])
    req = SwapRequest.model_validate(request["swap"])
    work = Path(s.WORK_DIR) / job_id
    work.mkdir(parents=True, exist_ok=True)
    run = Run(job_id, work)
    started = time.perf_counter()
    storage = get_storage()
    try:
        # 1. Load the saved state, the narration and every OTHER scene's segments.
        with run.stage("load", 5):
            state = read_state(root)
            precheck(state, index, req)
            data = _load(state)
            ratios = state["aspect_ratios"]
            storage.get_file(cache_key(root, "narration.wav"), work / "narration.wav")
            for scene_index, per_ratio in data.segments.items():
                if scene_index == index:
                    continue
                for ratio, facts in per_ratio.items():
                    folder = work / f"render_{ratio_slug(ratio)}"
                    folder.mkdir(parents=True, exist_ok=True)
                    storage.get_file(cache_key(root, f"{folder.name}/{facts['file']}"), folder / facts["file"])

        # 2. The new clip for this one scene.
        scene, old = data.scenes[index], data.selections[index]
        ctx = SelectionContext(work=work, orientation=orientation_of(ratios[0]), warn=run.warn, scorer=get_scorer(),
                               used=set(state["used"]))
        with run.stage("footage", 20):
            new = choose_clip(req, scene, data.meanings[index], data.plans[index], ctx, old,
                              {sel.candidate.uid: i for i, sel in enumerate(data.selections)})
            run.models["ranker"] = ctx.scorer.info()

        # 3. Render ONLY this scene (every ratio), then join each ratio with the cached segments.
        with run.stage("render", 50):
            chosen = [new]
            rendered = _render_segments(run, [scene], data.cues, ratios, chosen, [data.meanings[index]],
                                        [data.plans[index]], ctx, burn_captions=state["captions"],
                                        retry_with_next=req.choice == "next_best")
            data.selections[index] = chosen[0]  # a QA retry may have picked another clip
            data.segments[index] = rendered[index]
            set_progress(job_id, 75, "render")
            audio = SimpleNamespace(path=work / "narration.wav", duration=state["duration"])
            videos, qa_report = [], {}
            for ratio in ratios:
                videos.append(_join_video(run, data.scenes, data.segments, ratio, audio))
                qa_report[ratio] = videos[-1].pop("qa")

        # 4. New timeline.json + manifest.json, then upload (captions.srt does not change).
        with run.stage("upload", 90):
            latest = state["latest_job_id"]
            storage.get_file(output_key(latest, "captions.srt"), work / "captions.srt")
            storage.get_file(output_key(latest, "manifest.json"), work / "previous_manifest.json")
            previous_manifest = json.loads((work / "previous_manifest.json").read_text(encoding="utf-8"))
            record = _swap_record(job_id, index, req, old, data.selections[index], run, state, started,
                                  segments_rerendered=len(ratios), segments_reused=(len(data.scenes) - 1) * len(ratios))
            history = state["swaps"] + [record]
            timeline = build_timeline(job_id, state["input_type"], state["duration"], state["language"], data.scenes,
                                      data.meanings, data.plans, data.selections, data.segments, videos)
            timeline.update(root_job_id=root, version=len(history) + 1, swaps=history)
            write_json(timeline, work / "timeline.json")
            write_json(_manifest(previous_manifest, job_id, root, history, run, data.selections, videos, qa_report),
                       work / "manifest.json")
            files = [v["file"] for v in videos] + ["captions.srt", "timeline.json", "manifest.json"]
            for name in files:
                storage.put_file(output_key(job_id, name), work / name)

        # 5. The new segments join the cache; state.json now describes this latest version.
        with run.stage("cache", 97):
            for ratio, facts in data.segments[index].items():
                folder = f"render_{ratio_slug(ratio)}"
                storage.put_file(cache_key(root, f"{folder}/{facts['file']}"), work / folder / facts["file"])
            state.update(latest_job_id=job_id, swaps=history, used=sorted(set(state["used"]) | ctx.used),
                         segments={str(k): v for k, v in data.segments.items()},
                         selections=[sel.to_state() for sel in data.selections])
            storage.put_bytes(cache_key(root, "state.json"), json.dumps(state, ensure_ascii=False).encode("utf-8"))
        return files
    finally:
        if not s.KEEP_WORK_FILES:
            shutil.rmtree(work, ignore_errors=True)


def _clip_facts(sel: Selection) -> dict:
    c = sel.candidate
    return {"uid": c.uid, "provider": c.provider, "clip_id": c.id, "relevance_score": sel.clip_score}


def _swap_record(job_id, index, req, old, new, run, state, started, segments_rerendered, segments_reused) -> dict:
    """One entry of the swap history: what changed, why, and how long it took next to the full render."""
    seconds = round(time.perf_counter() - started, 3)  # same stopwatch point as the full job (before upload)
    full = state["full_render"]["seconds"]
    return {"job_id": job_id, "scene_index": index, "choice": req.choice,
            "request": req.model_dump(mode="json", exclude_none=True, exclude_defaults=True),
            "from": _clip_facts(old), "to": _clip_facts(new), "reason": new.reason,
            "swap_seconds": seconds, "swap_timings_seconds": dict(run.timings),
            "full_render_seconds": full, "speedup": round(full / seconds, 1) if seconds else None,
            "timing_note": "both measured from job start until every output file was written (upload excluded)",
            "segments_rerendered": segments_rerendered, "segments_reused": segments_reused,
            "finished_at": datetime.now(timezone.utc).isoformat()}


def _manifest(previous: dict, job_id, root, history, run, selections, videos, qa_report) -> dict:
    manifest = dict(previous)
    manifest.update(
        job_id=job_id, root_job_id=root, version=len(history) + 1,
        updated_at=datetime.now(timezone.utc).isoformat(),
        warnings=list(dict.fromkeys(previous.get("warnings", []) + run.warnings)),
        rights=rights_summary(selections), qa=qa_report, outputs=videos, swaps=history)
    manifest["models"] = {**previous.get("models", {}), **run.models}
    manifest["ai_generated"] = {**previous.get("ai_generated", {}),
                                "visuals": any(sel.candidate.ai_generated for sel in selections),
                                "generated_cards": sum(sel.candidate.generated for sel in selections)}
    for name, info in (manifest.get("providers") or {}).items():
        info["scenes_used"] = sum(sel.candidate.provider == name for sel in selections)
    return manifest
