"""Where job files live. Two backends with the SAME methods:

- LocalStorage: a folder on disk (STORAGE_BACKEND=local, for running on my PC).
- S3Storage:    MinIO / any S3 bucket (STORAGE_BACKEND=s3, for Docker, CI, deployment).

The rest of the app only calls get_storage() and never cares which one it got.
Keys look like "jobs/<job_id>/output/result.txt".
"""
import shutil
from collections.abc import Iterator
from pathlib import Path

from app.config import Settings, get_settings

CHUNK_SIZE = 1024 * 1024  # stream files in 1 MB pieces


class StorageNotFound(Exception):
    """The requested key does not exist."""


class Storage:
    name = "base"

    def put_file(self, key: str, local_path: str | Path) -> None: ...
    def put_bytes(self, key: str, data: bytes) -> None: ...
    def open_stream(self, key: str) -> tuple[Iterator[bytes], int]: ...  # (chunks, size in bytes)
    def exists(self, key: str) -> bool: ...
    def delete(self, key: str) -> None: ...
    def check(self) -> None: ...  # raise if storage is not usable (used by /ready)


class LocalStorage(Storage):
    name = "local"

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        # Safety: a key like "../../secret" must never escape the storage folder.
        if self.root != path and self.root not in path.parents:
            raise ValueError(f"invalid storage key: {key}")
        return path

    def put_file(self, key, local_path):
        dest = self._path(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(local_path, dest)

    def put_bytes(self, key, data):
        dest = self._path(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)

    def open_stream(self, key):
        path = self._path(key)
        if not path.is_file():
            raise StorageNotFound(key)

        def chunks():
            with path.open("rb") as f:
                while chunk := f.read(CHUNK_SIZE):
                    yield chunk

        return chunks(), path.stat().st_size

    def exists(self, key):
        return self._path(key).is_file()

    def delete(self, key):
        self._path(key).unlink(missing_ok=True)

    def check(self):
        # Ready = the folder exists (or can be made) and we can write to it.
        self.root.mkdir(parents=True, exist_ok=True)
        probe = self.root / ".ready_probe"
        probe.write_bytes(b"ok")
        probe.unlink()


class S3Storage(Storage):
    name = "s3"

    def __init__(self, endpoint_url: str, access_key: str, secret_key: str, bucket: str, region: str):
        import boto3
        from botocore.config import Config

        self.bucket = bucket
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint_url or None,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
            # path-style URLs work with MinIO; short timeouts so /ready answers fast.
            config=Config(s3={"addressing_style": "path"}, connect_timeout=3, read_timeout=30,
                          retries={"max_attempts": 2}),
        )

    def put_file(self, key, local_path):
        self.client.upload_file(str(local_path), self.bucket, key)

    def put_bytes(self, key, data):
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data)

    def open_stream(self, key):
        from botocore.exceptions import ClientError

        try:
            obj = self.client.get_object(Bucket=self.bucket, Key=key)
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                raise StorageNotFound(key) from e
            raise
        return obj["Body"].iter_chunks(CHUNK_SIZE), obj["ContentLength"]

    def exists(self, key):
        from botocore.exceptions import ClientError

        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError:
            return False

    def delete(self, key):
        self.client.delete_object(Bucket=self.bucket, Key=key)

    def check(self):
        # Ready = MinIO answers and the bucket exists.
        self.client.head_bucket(Bucket=self.bucket)


def build_storage(settings: Settings) -> Storage:
    if settings.STORAGE_BACKEND == "s3":
        return S3Storage(settings.S3_ENDPOINT_URL, settings.S3_ACCESS_KEY, settings.S3_SECRET_KEY,
                         settings.S3_BUCKET, settings.S3_REGION)
    return LocalStorage(settings.LOCAL_STORAGE_DIR)


_storage: Storage | None = None


def get_storage() -> Storage:
    global _storage
    if _storage is None:
        _storage = build_storage(get_settings())
    return _storage


def reset_storage() -> None:
    """Forget the storage object (tests use this after changing settings)."""
    global _storage
    _storage = None


def output_key(job_id: str, filename: str) -> str:
    return f"jobs/{job_id}/output/{filename}"


def input_key(job_id: str, filename: str) -> str:
    return f"jobs/{job_id}/input/{filename}"
