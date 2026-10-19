"""Specifications of the studio's object storage, against a fake of the S3 client."""

import io
import uuid
from typing import Any

import pytest
from botocore.exceptions import ClientError

from openmarketer_core.studio.asset_store import AssetStoreUnavailable, S3AssetStore, asset_key

SERVICE_SAYS = "Access Denied\nRequestId: 1234 Key: assets/secret"


class FakeS3:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], tuple[bytes, str]] = {}

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, ContentType: str) -> None:  # noqa: N803
        self.objects[(Bucket, Key)] = (Body, ContentType)

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)][0])}

    def delete_object(self, *, Bucket: str, Key: str) -> None:  # noqa: N803
        self.objects.pop((Bucket, Key), None)


class RefusingS3:
    def _refuse(self, **_: object) -> None:
        raise ClientError({"Error": {"Code": "AccessDenied", "Message": SERVICE_SAYS}}, "op")

    put_object = get_object = delete_object = _refuse


def test_the_key_of_a_version_is_under_its_run() -> None:
    ws, project, run, version = (uuid.uuid4() for _ in range(4))

    key = asset_key(
        workspace_id=ws, project_id=project, run_id=run, version_id=version, mime="image/jpeg"
    )

    assert key == f"assets/{ws}/{project}/{run}/{version}.jpg"


def test_an_image_is_stored_with_its_type_and_read_back() -> None:
    client = FakeS3()
    store = S3AssetStore(client, bucket="assets")

    store.put("assets/a.png", b"\x89PNG", content_type="image/png")

    assert client.objects[("assets", "assets/a.png")] == (b"\x89PNG", "image/png")
    assert store.get("assets/a.png") == b"\x89PNG"


def test_an_image_is_removed_and_removing_nothing_is_no_failure() -> None:
    client = FakeS3()
    store = S3AssetStore(client, bucket="assets")
    store.put("assets/a.png", b"\x89PNG", content_type="image/png")

    store.remove("assets/a.png")
    store.remove("assets/a.png")

    assert client.objects == {}


def test_a_refusal_is_unavailable_without_what_the_service_wrote() -> None:
    store = S3AssetStore(RefusingS3(), bucket="assets")

    for attempt in (
        lambda: store.put("k", b"x", content_type="image/png"),
        lambda: store.get("k"),
        lambda: store.remove("k"),
    ):
        with pytest.raises(AssetStoreUnavailable) as refused:
            attempt()
        assert "RequestId" not in str(refused.value)
        assert "secret" not in str(refused.value)
