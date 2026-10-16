"""The `mock` source for tests: no network, no keys.

search() returns made-up candidates whose tags are the query's words (so the mock scorer
can rank them). fetch() makes a real little video with FFmpeg's test pattern.
Tests change the class settings to simulate trouble:
    fail = True          -> every search raises ProviderError ("provider is down")
    corrupt_ids = {...}  -> fetch() writes garbage bytes for these ids ("corrupted download")
    clip_seconds = 6.0   -> length of every mock clip
    logo_probs = {id: 0.9} -> the mock scorer's logo probability for these ids ("a logo is visible")
    frame_logo_probs = {id: [0.05, 0.9, 0.05]} -> logo probability per frame of the DOWNLOADED clip
                                                  ("the thumbnail is clean, but frame 2 shows a logo")
"""
import hashlib
from pathlib import Path

from app.config import get_settings
from app.pipeline import media
from app.pipeline.providers.base import Candidate, ClipNotUsable, Provider, ProviderError

PER_QUERY = 3


class MockProvider(Provider):
    name = "mock"
    api_info = "local test clips (FFmpeg testsrc2)"
    fail = False
    corrupt_ids: set[str] = set()
    clip_seconds = 6.0
    logo_probs: dict[str, float] = {}
    frame_logo_probs: dict[str, list[float]] = {}

    def search(self, query, orientation, min_duration):
        if self.fail:
            raise ProviderError("mock provider is down")
        seed = get_settings().SEED
        out = []
        for i in range(PER_QUERY):
            clip_id = hashlib.sha1(f"{self.name}|{seed}|{query}|{i}".encode()).hexdigest()[:10]
            out.append(self._candidate(clip_id, query, i))
        return out

    def _candidate(self, clip_id: str, query: str, rank: int) -> Candidate:
        extra = {"rank_in_query": rank}
        if clip_id in self.logo_probs:
            extra["logo_prob"] = self.logo_probs[clip_id]
        if clip_id in self.frame_logo_probs:
            extra["frame_logo_probs"] = list(self.frame_logo_probs[clip_id])
        return Candidate(
            provider=self.name, id=clip_id, kind="video", preview_url=f"mock://preview/{clip_id}",
            download_url=f"mock://clip/{clip_id}", duration=self.clip_seconds, width=640, height=360,
            tags=query.lower().split() + ["mock"], title=f"mock clip {rank} for {query}",
            licence_name="Mock licence (tests only)", licence_url="https://example.com/mock-licence",
            attribution="mock", page_url=f"https://example.com/mock/{clip_id}", query=query, extra=extra)

    def lookup(self, clip_id: str) -> Candidate:
        """One clip by its id (clip swap with provider + clip_id). Any 10-hex-digit id exists."""
        if self.fail:
            raise ProviderError("mock provider is down")
        if len(clip_id) != 10 or any(ch not in "0123456789abcdef" for ch in clip_id):
            raise ClipNotUsable(f"mock clip {clip_id} not found")
        return self._candidate(clip_id, f"mock clip {clip_id}", 0)

    def fetch(self, candidate, dest_dir: Path) -> Path:
        dest = dest_dir / f"{self.name}_{candidate.id}.mp4"
        if candidate.id in self.corrupt_ids:
            dest.write_bytes(b"this is not a video file " * 200)
            return dest
        hue = int(candidate.id[:4], 16) % 360  # different colours per clip
        media.ffmpeg("-f", "lavfi", "-i", f"testsrc2=s=640x360:r=30:d={candidate.duration or self.clip_seconds}",
                     "-vf", f"hue=h={hue},format=yuv420p", "-c:v", "libx264", "-preset", "ultrafast",
                     "-an", str(dest))
        return dest

    def fetch_preview(self, candidate, dest_dir: Path) -> Path:
        """One frame of the same test pattern, as a JPEG (only used with RANK_SCORER=clip)."""
        dest = dest_dir / f"{self.name}_{candidate.id}.jpg"
        hue = int(candidate.id[:4], 16) % 360
        media.ffmpeg("-f", "lavfi", "-i", "testsrc2=s=320x180:r=1:d=1", "-vf", f"hue=h={hue}", "-frames:v", "1",
                     str(dest))
        return dest
