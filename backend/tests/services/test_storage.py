# [plan-fix] plan shipped storage.py with no test; TDD iron law requires one.
# MinIO is a true boundary (kb-tdd-workflow), so the client is faked; behaviour
# against a live MinIO is verified on the Docker stack.
import io

import pytest

from app.services import storage


class FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.closed = False
        self.released = False

    def read(self) -> bytes:
        return self.payload

    def close(self) -> None:
        self.closed = True

    def release_conn(self) -> None:
        self.released = True


class FakeMinio:
    def __init__(self, existing_buckets: set[str] | None = None) -> None:
        self.buckets = existing_buckets or set()
        self.objects: dict[tuple[str, str], bytes] = {}
        self.last_response: FakeResponse | None = None

    def bucket_exists(self, bucket: str) -> bool:
        return bucket in self.buckets

    def make_bucket(self, bucket: str) -> None:
        self.buckets.add(bucket)

    def put_object(self, bucket, object_name, data, length, content_type=None):
        self.objects[(bucket, object_name)] = data.read(length)

    def get_object(self, bucket: str, key: str) -> FakeResponse:
        self.last_response = FakeResponse(self.objects[(bucket, key)])
        return self.last_response


@pytest.fixture
def fake_minio(monkeypatch):
    fake = FakeMinio()
    monkeypatch.setattr(storage, "_client", lambda: fake)
    return fake


def test_upload_creates_bucket_and_returns_object_path(fake_minio):
    path = storage.upload_file("run1/notes.md", io.BytesIO(b"# hi"), 4)
    assert path == "kb-uploads/run1/notes.md"
    assert "kb-uploads" in fake_minio.buckets
    assert fake_minio.objects[("kb-uploads", "run1/notes.md")] == b"# hi"


def test_download_splits_bucket_and_key_and_releases_conn(fake_minio):
    fake_minio.objects[("kb-uploads", "run1/notes.md")] = b"# hi"
    data = storage.download_file("kb-uploads/run1/notes.md")
    assert data == b"# hi"
    assert fake_minio.last_response.closed
    assert fake_minio.last_response.released


def test_ensure_bucket_skips_existing(fake_minio):
    fake_minio.buckets.add("kb-uploads")
    storage.ensure_bucket("kb-uploads")  # must not raise / recreate
    assert fake_minio.buckets == {"kb-uploads"}
