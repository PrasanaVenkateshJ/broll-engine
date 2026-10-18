"""Footage stage speed-ups must not change WHAT is chosen.

- pass 1 (parallel) + pass 2 (one scene at a time) gives the same clips for 1 or 4 workers,
- no clip is used twice, even with many scenes searching at the same time,
- the queries of a scene really run at the same time,
- search results + preview pictures are fetched once per job, and reused by later jobs,
- the lazy logo check picks exactly what checking every candidate would pick.
"""
import threading

import pytest

from app.config import get_settings
from app.pipeline import footage_cache, rank
from app.pipeline.providers import REGISTRY
from app.pipeline.providers.mock import MockProvider
from app.pipeline.rank import MockScorer, SelectionContext, get_scorer, select_clips
from app.pipeline.safety import plan_search
from app.pipeline.scenes import Scene
from app.pipeline.understand import MockUnderstander

SCRIPT = ["Small shops open early every morning.", "Small shops open late on Fridays.",
          "Busy shops sell fresh bread.", "Small shops close early in winter.",
          "Fresh bread in small shops.", "Shops open early again."]


def setenv(monkeypatch, **values):
    for key, value in values.items():
        monkeypatch.setenv(key, str(value))
    get_settings.cache_clear()


def make(texts):
    scenes = [Scene(i, i * 4.0, i * 4.0 + 4.0, t) for i, t in enumerate(texts)]
    meanings = MockUnderstander().understand(scenes, " ".join(texts))
    return scenes, meanings, [plan_search(m, sc.text) for m, sc in zip(meanings, scenes)]


def run(tmp_path, texts=SCRIPT, scorer=None):
    scenes, meanings, plans = make(texts)
    ctx = SelectionContext(work=tmp_path, orientation="landscape", warn=lambda m: None,
                           scorer=scorer or get_scorer())
    selections, facts = select_clips(scenes, meanings, plans, ctx)
    return selections, facts, ctx


def summary(selections):
    return [(s.candidate.uid, s.clip_score, s.final_score, s.reason) for s in selections]


def test_same_choices_with_one_or_four_footage_workers(tmp_path, monkeypatch):
    setenv(monkeypatch, FOOTAGE_WORKERS=1)
    one, facts_one, _ = run(tmp_path / "a")
    setenv(monkeypatch, FOOTAGE_WORKERS=4)
    four, facts_four, _ = run(tmp_path / "b")
    assert summary(one) == summary(four)
    assert facts_one["footage_workers"] == 1 and facts_four["footage_workers"] == 4


def test_no_clip_is_used_twice_when_scenes_search_in_parallel(tmp_path, monkeypatch):
    # Every scene finds the SAME candidates (same words), so all of them want the same best clip.
    setenv(monkeypatch, FOOTAGE_WORKERS=6)
    selections, _, _ = run(tmp_path, texts=["Small shops open early."] * 6)
    uids = [s.candidate.uid for s in selections]
    assert len(uids) == len(set(uids))


class CountingProvider(MockProvider):
    """A mock source that counts its calls and may be cached across jobs, like a real API."""
    name = "counting"
    cache_across_jobs = True
    searches: list[str] = []
    previews: list[str] = []
    lock = threading.Lock()

    def search(self, query, orientation, min_duration):
        with self.lock:
            self.searches.append(query)
        return super().search(query, orientation, min_duration)

    def fetch_preview(self, candidate, dest_dir):
        with self.lock:
            self.previews.append(candidate.uid)
        return super().fetch_preview(candidate, dest_dir)


class PictureScorer(MockScorer):
    """The mock scorer, but it asks for preview pictures (like CLIP does)."""
    needs_images = True


@pytest.fixture
def counting(monkeypatch):
    monkeypatch.setitem(REGISTRY, "counting", CountingProvider)
    monkeypatch.setattr(CountingProvider, "searches", [])
    monkeypatch.setattr(CountingProvider, "previews", [])
    setenv(monkeypatch, PROVIDER_ORDER="counting,generated")
    return CountingProvider


def test_each_query_and_preview_is_fetched_once_per_job(tmp_path, counting):
    texts = ["Small shops open early.", "Small shops open early."]  # same queries, same length
    run(tmp_path, texts=texts, scorer=PictureScorer())
    assert len(counting.searches) == len(set(counting.searches))
    assert len(counting.previews) == len(set(counting.previews))


def test_a_later_job_reuses_cached_searches_and_previews(tmp_path, counting):
    first, _, _ = run(tmp_path / "a", scorer=PictureScorer())
    searches, previews = len(counting.searches), len(counting.previews)
    assert searches and previews
    second, _, _ = run(tmp_path / "b", scorer=PictureScorer())
    assert len(counting.searches) == searches and len(counting.previews) == previews  # nothing new fetched
    assert summary(first) == summary(second)


def test_the_cross_job_cache_can_be_switched_off(tmp_path, counting, monkeypatch):
    setenv(monkeypatch, FOOTAGE_CACHE_SECONDS=0)
    run(tmp_path / "a")
    searches = len(counting.searches)
    run(tmp_path / "b")
    assert len(counting.searches) == 2 * searches


def test_a_provider_error_is_not_cached_for_later_jobs(tmp_path, counting, monkeypatch):
    monkeypatch.setattr(CountingProvider, "fail", True)
    failed, _, _ = run(tmp_path / "a", texts=SCRIPT[:1])
    assert failed[0].candidate.provider == "generated"
    monkeypatch.setattr(CountingProvider, "fail", False)
    ok, _, _ = run(tmp_path / "b", texts=SCRIPT[:1])
    assert ok[0].candidate.provider == "counting"


def test_the_queries_of_one_scene_are_searched_at_the_same_time(tmp_path, monkeypatch):
    barrier = threading.Barrier(3, timeout=5)  # only passes if all 3 queries are in search() at once

    class Together(MockProvider):
        name = "together"

        def search(self, query, orientation, min_duration):
            barrier.wait()
            return super().search(query, orientation, min_duration)

    monkeypatch.setitem(REGISTRY, "together", Together)
    setenv(monkeypatch, PROVIDER_ORDER="together,generated", QUERIES_PER_SCENE=3)
    selections, _, _ = run(tmp_path, texts=["Small shops open early every morning."])
    assert selections[0].candidate.provider == "together"


def test_lazy_logo_check_picks_the_same_clips_as_checking_everything(tmp_path, monkeypatch):
    monkeypatch.setenv("EARLY_EXIT", "false")  # early exit already logo-checks while scoring: compare without it
    get_settings.cache_clear()
    scenes, _, plans = make(SCRIPT)
    # A few logos (rejected) and possible logos (penalised) among the candidates.
    ids = sorted({c.id for p in plans for q in p.queries for c in MockProvider().search(q, "landscape", 4)})
    monkeypatch.setattr(MockProvider, "logo_probs", {i: [0.9, 0.5, 0.05][n % 3] for n, i in enumerate(ids)})
    checked = []
    original = MockScorer.logo_probs

    def counting_logo_probs(self, candidates, previews):
        checked.extend(c.uid for c in candidates)
        return original(self, candidates, previews)

    monkeypatch.setattr(MockScorer, "logo_probs", counting_logo_probs)
    lazy, _, _ = run(tmp_path / "lazy")
    lazy_checks = len(checked)
    checked.clear()
    monkeypatch.setattr(rank, "LOGO_BATCH", 10_000)  # = check every passing candidate first
    full, _, _ = run(tmp_path / "full")
    # Same clips, scores and logo results. (Only the reason's "rejected N (M checked)" count may differ.)
    same = [(s.candidate.uid, s.clip_score, s.final_score, s.logo_check) for s in lazy]
    assert same == [(s.candidate.uid, s.clip_score, s.final_score, s.logo_check) for s in full]
    assert lazy_checks < len(checked)


def test_footage_cache_prune_removes_old_previews(tmp_path, monkeypatch):
    setenv(monkeypatch, FOOTAGE_CACHE_SECONDS=60)
    old = footage_cache.preview_file("https://example.com/a.jpg")
    old.parent.mkdir(parents=True)
    old.write_bytes(b"x")
    import os
    import time
    os.utime(old, (time.time() - 3600, time.time() - 3600))
    assert footage_cache.prune() == 1 and not old.exists()
