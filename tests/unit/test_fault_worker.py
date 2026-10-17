"""The resilience test tool corrupts exactly the first N downloads of one provider (and nothing else)."""
from app.pipeline import media
from app.pipeline.providers import REGISTRY
from tools.fault_worker import corrupt_downloads


def test_only_the_first_download_is_corrupted(tmp_path, monkeypatch):
    mock = REGISTRY["mock"]
    monkeypatch.setattr(mock, "fetch", mock.fetch)  # put the real method back after the test
    state = corrupt_downloads("mock", count=1)
    provider = mock()
    first, second = provider.search("calm sea", "landscape", 2.0)[:2]
    broken = provider.fetch(first, tmp_path)
    fine = provider.fetch(second, tmp_path)
    assert media.probe(broken) is None          # ffprobe cannot read the cut file
    assert media.probe(fine) is not None
    assert state["corrupted"] == [first.uid] and state["left"] == 0
