"""Real MinIO round trip. Runs only where STORAGE_BACKEND=s3 is set (Docker / CI); skipped on my PC."""
import os

import pytest

# Read BEFORE the test fixture swaps the settings to local storage.
_S3 = {k: os.environ.get(k) for k in ("STORAGE_BACKEND", "S3_ENDPOINT_URL", "S3_ACCESS_KEY", "S3_SECRET_KEY",
                                      "S3_BUCKET", "S3_REGION")}

pytestmark = pytest.mark.skipif(_S3["STORAGE_BACKEND"] != "s3", reason="needs STORAGE_BACKEND=s3 (MinIO)")


def test_s3_put_stream_delete():
    from app.storage import S3Storage, StorageNotFound

    store = S3Storage(_S3["S3_ENDPOINT_URL"], _S3["S3_ACCESS_KEY"], _S3["S3_SECRET_KEY"],
                      _S3["S3_BUCKET"], _S3["S3_REGION"] or "us-east-1")
    store.check()
    key = "tests/roundtrip.txt"
    store.put_bytes(key, b"hello minio")
    chunks, size = store.open_stream(key)
    assert b"".join(chunks) == b"hello minio" and size == 11
    store.delete(key)
    with pytest.raises(StorageNotFound):
        store.open_stream(key)
