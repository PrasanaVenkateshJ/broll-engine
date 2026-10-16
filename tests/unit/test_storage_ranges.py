"""LocalStorage sends exactly the asked-for bytes (the S3 version is checked against MinIO in
tests/integration/test_s3_storage.py)."""
import pytest

from app.storage import CHUNK_SIZE, LocalStorage, StorageNotFound


def test_local_storage_ranges(tmp_path):
    store = LocalStorage(tmp_path)
    data = bytes(range(256)) * (CHUNK_SIZE // 256 * 2 + 3)  # a bit more than 2 chunks
    store.put_bytes("k.bin", data)
    assert store.size("k.bin") == len(data)
    for start, end in [(0, None), (0, 0), (5, 9), (CHUNK_SIZE - 2, CHUNK_SIZE + 2), (len(data) - 3, None),
                       (10, len(data) * 2)]:
        chunks, total = store.open_stream("k.bin", start, end)
        want = data[start:] if end is None else data[start:end + 1]
        assert b"".join(chunks) == want and total == len(data), (start, end)


def test_local_storage_size_of_missing_key(tmp_path):
    with pytest.raises(StorageNotFound):
        LocalStorage(tmp_path).size("nope")
