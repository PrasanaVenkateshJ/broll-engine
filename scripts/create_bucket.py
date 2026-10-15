"""Used by the `minio-init` compose service: wait for MinIO, then create the bucket.

Run: python scripts/create_bucket.py   (reads S3_* settings from env)
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # so "import app" works

from botocore.exceptions import ClientError, EndpointConnectionError  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.storage import S3Storage  # noqa: E402


def main(wait_seconds: int = 60) -> int:
    s = get_settings()
    store = S3Storage(s.S3_ENDPOINT_URL, s.S3_ACCESS_KEY, s.S3_SECRET_KEY, s.S3_BUCKET, s.S3_REGION)
    deadline = time.time() + wait_seconds
    while True:
        try:
            try:
                store.check()
                print(f"bucket '{s.S3_BUCKET}' already exists")
            except ClientError:
                store.client.create_bucket(Bucket=s.S3_BUCKET)
                print(f"bucket '{s.S3_BUCKET}' created")
            return 0
        except (EndpointConnectionError, ConnectionError, ClientError) as e:
            if time.time() > deadline:
                print(f"MinIO not reachable at {s.S3_ENDPOINT_URL}: {e}", file=sys.stderr)
                return 1
            print("waiting for MinIO ...")
            time.sleep(2)


if __name__ == "__main__":
    sys.exit(main())
