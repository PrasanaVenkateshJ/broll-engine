"""Pixabay (one key, PIXABAY_API_KEY, for both APIs).

- pixabay_video:        Pixabay Videos API -> real stock video clips.
- pixabay_image_motion: Pixabay Images API -> a still photo; render.py turns it into a
                        clip with a slow Ken Burns zoom/pan.

Licence: Pixabay Content License - commercial use allowed, no attribution required, no
watermark on files downloaded through the API. We still record the creator's name.
"""
from app.config import get_settings
from app.pipeline.providers.base import Candidate, Provider, ProviderError, get_json

LICENCE_NAME = "Pixabay Content License"
LICENCE_URL = "https://pixabay.com/service/license-summary/"
# Pixabay video sizes: large is often 4K, medium ~1920x1080, small ~1280x720, tiny ~960x540.
VIDEO_SIZE_PREFERENCE = ("medium", "large", "small", "tiny")


def _tags(text: str) -> list[str]:
    return [t.strip().lower() for t in (text or "").split(",") if t.strip()]


class PixabayVideoProvider(Provider):
    name = "pixabay_video"
    key_setting = "PIXABAY_API_KEY"
    api_info = "Pixabay Videos API (pixabay.com/api/videos)"

    def search(self, query, orientation, min_duration):
        s = get_settings()
        data = get_json(s.PIXABAY_VIDEO_API_URL, {
            "key": s.PIXABAY_API_KEY, "q": query[:100], "safesearch": "true", "video_type": "film",
            "per_page": max(3, min(200, s.PROVIDER_RESULTS_PER_QUERY)), "order": "popular"})
        hits = data.get("hits")
        if not isinstance(hits, list):
            raise ProviderError("unexpected answer (no hits)")
        out = []
        max_bytes = s.MAX_CLIP_DOWNLOAD_MB * 1024 * 1024
        for hit in hits:
            videos = hit.get("videos") or {}
            chosen = next((videos[k] for k in VIDEO_SIZE_PREFERENCE
                           if (videos.get(k) or {}).get("url") and int(videos[k].get("size") or 0) <= max_bytes), None)
            duration = float(hit.get("duration") or 0)
            if not chosen or duration < max(1.0, min_duration * 0.5):  # would loop more than twice
                continue
            preview = (videos.get("small") or {}).get("thumbnail") or chosen.get("thumbnail") or ""
            if not preview:
                continue  # nothing to score
            out.append(Candidate(
                provider=self.name, id=str(hit.get("id")), kind="video", preview_url=preview,
                download_url=chosen["url"], duration=duration, width=int(chosen.get("width") or 0),
                height=int(chosen.get("height") or 0), tags=_tags(hit.get("tags", "")),
                licence_name=LICENCE_NAME, licence_url=LICENCE_URL,
                attribution=f"{hit.get('user', 'unknown')} on Pixabay", page_url=hit.get("pageURL", ""),
                query=query))
        return out


class PixabayImageProvider(Provider):
    name = "pixabay_image_motion"
    key_setting = "PIXABAY_API_KEY"
    api_info = "Pixabay Images API (pixabay.com/api) + FFmpeg Ken Burns motion"

    def search(self, query, orientation, min_duration):
        s = get_settings()
        data = get_json(s.PIXABAY_IMAGE_API_URL, {
            "key": s.PIXABAY_API_KEY, "q": query[:100], "safesearch": "true", "image_type": "photo",
            "orientation": {"landscape": "horizontal", "portrait": "vertical"}.get(orientation, "all"),
            "per_page": max(3, min(200, s.PROVIDER_RESULTS_PER_QUERY)), "order": "popular"})
        hits = data.get("hits")
        if not isinstance(hits, list):
            raise ProviderError("unexpected answer (no hits)")
        out = []
        for hit in hits:
            if not hit.get("largeImageURL") or not hit.get("webformatURL"):
                continue
            # The API gives the ORIGINAL size; the large file is at most 1280 px on its long side.
            w, h = int(hit.get("imageWidth") or 0), int(hit.get("imageHeight") or 0)
            scale = min(1.0, 1280 / max(w, h)) if w and h else 1.0
            out.append(Candidate(
                provider=self.name, id=str(hit.get("id")), kind="image", preview_url=hit["webformatURL"],
                download_url=hit["largeImageURL"], width=round(w * scale), height=round(h * scale),
                tags=_tags(hit.get("tags", "")), licence_name=LICENCE_NAME, licence_url=LICENCE_URL,
                attribution=f"{hit.get('user', 'unknown')} on Pixabay", page_url=hit.get("pageURL", ""),
                query=query))
        return out
