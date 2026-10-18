"""Independent vision-language judge: a second opinion on the top footage candidates of a scene.

CLIP (rank.py) compares a picture with short queries. The judge is a different kind of model: it READS the
narration line and LOOKS at the candidates, and rates each one 1-5 ("5 = shows what the line means,
1 = unrelated or unsuitable"). rank.py uses the ratings in two measurable ways:
- re-rank: among the judged candidates of a source, the higher rating goes first (CLIP order breaks ties);
  a rating <= VLM_REJECT_SCORE drops the candidate;
- adaptive threshold: a candidate just below STOCK_MIN_SCORE may be used only if the judge rates it
  VLM_ACCEPT_SCORE or more.

Adapters (VLM_PROVIDER), same pattern as tts.py / understand.py:
- GroqJudge: a vision model on Groq (VLM_MODEL, default qwen/qwen3.8-27b, Apache-2.0 weights; a Groq
  "preview" model). The VLM_TOP_K candidates are drawn side by side in ONE picture with labels A, B, C:
  Groq counts every image as 2,048 tokens and the free tier allows 8,000 tokens per minute, so one picture
  per scene is the affordable way. Temperature 0 + a fixed seed.
- MockJudge: no network (tests). Rating from the candidate's tags vs the queries, or extra["vlm_score"].
- none: no judge (the default): no re-rank, no adaptive pass.

A judge problem never fails a job: the scene is simply not judged, with a warning.
"""
import base64
import io
import json
import logging
import threading
import time
from pathlib import Path

import httpx

from app.config import get_settings

log = logging.getLogger("app.pipeline.vlm")

LABELS = "ABCDE"
SYSTEM_PROMPT = """You check B-roll (background footage) for a narrated video. You get ONE narration line
and one picture made of several candidate frames side by side, labelled A, B, C ... in their top-left
corner. Rate EACH candidate 1-5 as B-roll for this line: 5 = clearly shows what the line means (its
meaning, not just its words), 4 = good fit, 3 = loosely related, 2 = weak, 1 = unrelated, misleading,
graphic, or shows a readable brand logo. Judge only what is visible. Answer with JSON only:
{"ratings": {"A": 4, "B": 2}, "why": "one short sentence"}"""

_lock = threading.Lock()
_disabled_until = 0.0  # process-wide: after a long rate limit (e.g. the daily cap) the judge sits out


class JudgeError(Exception):
    """The judge could not answer for this scene."""


class JudgeUnavailable(JudgeError):
    """The judge cannot answer for a while (rate limit longer than VLM_MAX_RETRY_AFTER_SECONDS)."""


def reset() -> None:
    global _disabled_until
    with _lock:
        _disabled_until = 0.0


def _disable_for(seconds: float) -> None:
    global _disabled_until
    with _lock:
        _disabled_until = max(_disabled_until, time.monotonic() + seconds)


def disabled_for() -> float:
    with _lock:
        return max(0.0, _disabled_until - time.monotonic())


class Judge:
    name = "base"
    needs_images = True

    def judge(self, text: str, english: str | None, items: list[tuple]) -> tuple[dict[str, int], str]:
        """items = [(uid, candidate, preview path)] -> ({uid: rating 1-5}, why). Raises JudgeError."""
        raise NotImplementedError

    def info(self) -> dict:
        return {"provider": self.name}


class MockJudge(Judge):
    """Tests: extra["vlm_score"] if a test set one, else 4 when a query word is in the clip's tags, else 2."""
    name = "mock"
    needs_images = False

    def __init__(self, queries_by_text: dict[str, list[str]] | None = None):
        self.calls = 0

    def judge(self, text, english, items):
        self.calls += 1
        words = {w.strip(".,!?").lower() for w in (english or text).split() if len(w) > 3}
        out = {}
        for uid, cand, _ in items:
            if "vlm_score" in cand.extra:
                out[uid] = int(cand.extra["vlm_score"])
            else:
                out[uid] = 4 if words & {t.lower() for t in cand.tags} else 2
        return out, "mock: tags vs the line"

    def info(self):
        return {"provider": self.name, "calls": self.calls}


def contact_sheet(paths: list[Path], panel_width: int) -> bytes:
    """The pictures side by side (16:9 panels, letterboxed), each labelled A, B, C ... -> JPEG bytes."""
    from PIL import Image, ImageDraw

    panel_h = round(panel_width * 9 / 16)
    sheet = Image.new("RGB", (panel_width * len(paths), panel_h), (20, 20, 20))
    draw = ImageDraw.Draw(sheet)
    for i, path in enumerate(paths):
        picture = Image.open(path).convert("RGB")
        picture.thumbnail((panel_width - 4, panel_h - 4))
        x = i * panel_width + (panel_width - picture.width) // 2
        sheet.paste(picture, (x, (panel_h - picture.height) // 2))
        box = (i * panel_width + 2, 2, i * panel_width + 26, 24)
        draw.rectangle(box, fill=(255, 230, 0))
        draw.text((box[0] + 8, box[1] + 5), LABELS[i], fill=(0, 0, 0))
    buffer = io.BytesIO()
    sheet.save(buffer, "JPEG", quality=80)
    return buffer.getvalue()


class GroqJudge(Judge):
    name = "groq"

    def __init__(self):
        self.calls = 0
        self.tokens_used = 0
        self.rate_limited = 0
        self.waited = 0.0

    def info(self):
        s = get_settings()
        return {"provider": self.name, "model": s.VLM_MODEL, "endpoint": s.GROQ_BASE_URL, "temperature": 0,
                "seed": s.SEED, "top_k": s.VLM_TOP_K, "panel_width": s.VLM_PANEL_WIDTH, "calls": self.calls,
                "tokens_used": self.tokens_used, "rate_limited_answers": self.rate_limited,
                "seconds_waited_for_rate_limit": round(self.waited, 1),
                "model_licence": "Apache-2.0 (Qwen weights); Groq marks this model as a preview"}

    def judge(self, text, english, items):
        s = get_settings()
        if not s.GROQ_API_KEY:
            raise JudgeUnavailable("GROQ_API_KEY is empty")
        if left := disabled_for():
            raise JudgeUnavailable(f"paused after a rate limit, {left:.0f} s left")
        items = [it for it in items if it[2] is not None][:min(len(LABELS), max(1, s.VLM_TOP_K))]
        if not items:
            raise JudgeError("no candidate pictures to show")
        image = base64.b64encode(contact_sheet([p for _, _, p in items], s.VLM_PANEL_WIDTH)).decode()
        line = text if not english else f"{text}\n(English: {english})"
        user = (f"Narration line: {line}\nCandidates: {', '.join(LABELS[:len(items)])}. "
                "Rate each one 1-5 as JSON.")
        payload = {"model": s.VLM_MODEL, "temperature": 0, "seed": s.SEED, "max_completion_tokens": 200,
                   "response_format": {"type": "json_object"},
                   "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                                {"role": "user", "content": [
                                    {"type": "text", "text": user},
                                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image}"}}]}]}
        for attempt in range(2):  # one more try after a short rate-limit wait
            self.calls += 1
            try:
                resp = httpx.post(f"{s.GROQ_BASE_URL.rstrip('/')}/chat/completions", json=payload,
                                  headers={"Authorization": f"Bearer {s.GROQ_API_KEY}"}, timeout=s.VLM_TIMEOUT_SECONDS)
            except httpx.HTTPError as e:
                raise JudgeError(f"network error ({type(e).__name__})") from None
            if resp.status_code == 429:
                self.rate_limited += 1
                try:
                    wait = float(resp.headers.get("retry-after"))
                except (TypeError, ValueError):
                    wait = 5.0
                if wait > s.VLM_MAX_RETRY_AFTER_SECONDS or attempt == 1:
                    _disable_for(max(wait, 60.0))
                    raise JudgeUnavailable(f"HTTP 429 (rate limited, retry-after {wait:.0f} s)")
                time.sleep(wait)
                self.waited += wait
                continue
            if resp.status_code != 200:
                raise JudgeError(f"HTTP {resp.status_code}")
            body = resp.json()
            self.tokens_used += int((body.get("usage") or {}).get("total_tokens") or 0)
            try:
                answer = json.loads(body["choices"][0]["message"]["content"])
                ratings = answer["ratings"]
            except (KeyError, IndexError, TypeError, json.JSONDecodeError) as e:
                raise JudgeError("the answer was not valid JSON with ratings") from e
            out = {}
            for i, (uid, _, _) in enumerate(items):
                try:
                    out[uid] = max(1, min(5, int(round(float(ratings.get(LABELS[i]))))))
                except (TypeError, ValueError, AttributeError):
                    continue  # this candidate was not rated
            if not out:
                raise JudgeError("the answer rated no candidate")
            return out, str(answer.get("why") or "")[:200]
        raise JudgeError("rate limited")


def get_judge() -> Judge | None:
    """The configured judge, or None (VLM_PROVIDER=none, or QUALITY_MODE=fast)."""
    s = get_settings()
    if not s.vlm_enabled:
        return None
    name = s.VLM_PROVIDER
    if name == "groq":
        return GroqJudge()
    if name == "mock":
        return MockJudge()
    return None
