"""Pexels Videos API. Kept for later, but NOT needed: Pexels has paused new API keys.

With PEXELS_API_KEY empty this provider is simply skipped (see providers/__init__.py).
Licence: Pexels License - free for commercial use, no attribution required, no watermark.
"""
from app.config import get_settings
from app.pipeline.providers.base import Candidate, Provider, ProviderError, get_json

LICENCE_NAME = "Pexels License"
LICENCE_URL = "https://www.pexels.com/license/"


class PexelsProvider(Provider):
    name = "pexels"
    key_setting = "PEXELS_API_KEY"
    api_info = "Pexels Videos API (api.pexels.com/videos)"

    def search(self, query, orientation, min_duration):
        s = get_settings()
        params = {"query": query[:100], "per_page": max(1, min(80, s.PROVIDER_RESULTS_PER_QUERY))}
        if orientation in ("landscape", "portrait", "square"):
            params["orientation"] = orientation
        data = get_json(s.PEXELS_VIDEO_API_URL, params, headers={"Authorization": s.PEXELS_API_KEY})
        videos = data.get("videos")
        if not isinstance(videos, list):
            raise ProviderError("unexpected answer (no videos)")
        out = []
        for video in videos:
            files = [f for f in video.get("video_files") or [] if f.get("file_type") == "video/mp4" and f.get("link")]
            # The file closest to 1920 px wide (not bigger than needed).
            files.sort(key=lambda f: abs(int(f.get("width") or 0) - 1920))
            duration = float(video.get("duration") or 0)
            if not files or not video.get("image") or duration < max(1.0, min_duration * 0.5):
                continue
            best = files[0]
            out.append(Candidate(
                provider=self.name, id=str(video.get("id")), kind="video", preview_url=video["image"],
                download_url=best["link"], duration=duration, width=int(best.get("width") or 0),
                height=int(best.get("height") or 0),
                tags=[w for w in str(video.get("url", "")).rstrip("/").split("/")[-1].split("-") if w.isalpha()],
                licence_name=LICENCE_NAME, licence_url=LICENCE_URL,
                attribution=f"{(video.get('user') or {}).get('name', 'unknown')} on Pexels",
                page_url=video.get("url", ""), query=query))
        return out
