"""MinIO / S3-compatible object storage client.

Used for storing original uploaded files.
"""

from __future__ import annotations

from typing import BinaryIO

from minio import Minio

from app.core.config import settings


def _client() -> Minio:
    return Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=settings.minio_secure,
    )


def ensure_bucket(bucket: str = "kb-uploads") -> None:
    client = _client()
    if not client.bucket_exists(bucket):
        client.make_bucket(bucket)


def upload_file(
    object_name: str,
    data: BinaryIO,
    length: int,
    content_type: str = "application/octet-stream",
    bucket: str = "kb-uploads",
) -> str:
    """Upload to MinIO and return the object path."""
    ensure_bucket(bucket)
    client = _client()
    client.put_object(bucket, object_name, data, length, content_type=content_type)
    return f"{bucket}/{object_name}"


def download_file(object_path: str) -> bytes:
    """Download from MinIO. object_path = 'bucket/key'."""
    bucket, key = object_path.split("/", 1)
    client = _client()
    response = client.get_object(bucket, key)
    try:
        return response.read()
    finally:
        response.close()
        response.release_conn()
