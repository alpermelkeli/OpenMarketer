"""Keeping the images of the creative studio in the installation's object storage.

Every version of an asset is an image stored here and never in the database: rows keep what
a version is and how it was made (``db/media_assets.py``), and the object keeps its bytes.
Keys are ``assets/<workspace>/<project>/<run>/<version>.<ext>``, so the images of one run can
be found and removed without reading a row; the extension follows the image's type.

``S3AssetStore`` speaks the S3 API through boto3, against the same bucket and with the same
settings as the research pages (``research/page_store.py``): MinIO in the dev stack by default,
any S3-compatible service otherwise. The bucket is expected to exist; nothing here creates
one. An image is checked before it gets here (``output_check.py`` and the studio's own checks),
and the store adds no rule of its own: it stores bytes with their type, answers with bytes,
removes what it is told, and words every failure as unavailable without showing what the
service wrote.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from typing import Any, Protocol

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from openmarketer_core.studio.sandbox import ImageMime

logger = logging.getLogger(__name__)

ASSETS_PREFIX = "assets"
# The dev stack's MinIO, as the worker on the host reaches it; production sets its own.
DEFAULT_ENDPOINT_URL = "http://localhost:9000"
DEFAULT_BUCKET = "openmarketer-assets"
DEFAULT_REGION = "us-east-1"
DEFAULT_ACCESS_KEY = "openmarketer"
DEFAULT_SECRET_KEY = "openmarketer-dev"

EXTENSIONS: Mapping[ImageMime, str] = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
}


class AssetStoreUnavailable(Exception):
    """The object storage refused the work or could not be reached."""


class AssetStore(Protocol):
    """What the studio needs of object storage: store an image, read it back, remove it."""

    def put(self, key: str, body: bytes, *, content_type: ImageMime) -> None: ...

    def get(self, key: str) -> bytes: ...

    def remove(self, key: str) -> None: ...


def asset_key(
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    version_id: uuid.UUID,
    mime: ImageMime,
) -> str:
    """Where the image of one version lives: under its run, which is under its project."""
    return f"{ASSETS_PREFIX}/{workspace_id}/{project_id}/{run_id}/{version_id}.{EXTENSIONS[mime]}"


class S3AssetStore:
    """``AssetStore`` over an S3-compatible service, through boto3."""

    def __init__(self, client: Any, *, bucket: str) -> None:
        """``client`` is a boto3 S3 client (or a fake of one, in tests)."""
        self._client = client
        self._bucket = bucket

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> S3AssetStore:
        """The store the environment names; the defaults are the dev stack's MinIO."""
        client = boto3.client(
            "s3",
            endpoint_url=environ.get("S3_ENDPOINT_URL") or DEFAULT_ENDPOINT_URL,
            aws_access_key_id=environ.get("S3_ACCESS_KEY") or DEFAULT_ACCESS_KEY,
            aws_secret_access_key=environ.get("S3_SECRET_KEY") or DEFAULT_SECRET_KEY,
            region_name=environ.get("S3_REGION") or DEFAULT_REGION,
            # MinIO and most S3-compatible services are reached by path, not by bucket host.
            config=Config(s3={"addressing_style": "path"}),
        )
        return cls(client, bucket=environ.get("S3_BUCKET") or DEFAULT_BUCKET)

    def put(self, key: str, body: bytes, *, content_type: ImageMime) -> None:
        """Store ``body`` under ``key`` as an image of ``content_type``, replacing what is there."""
        try:
            self._client.put_object(
                Bucket=self._bucket, Key=key, Body=body, ContentType=content_type
            )
        except (ClientError, BotoCoreError) as e:
            logger.warning("an image was not stored: %s", _first_line(e))
            raise AssetStoreUnavailable("the object storage could not store an image") from e

    def get(self, key: str) -> bytes:
        """The bytes stored under ``key``. Raises ``AssetStoreUnavailable`` when unreadable."""
        try:
            return self._client.get_object(Bucket=self._bucket, Key=key)["Body"].read()
        except (ClientError, BotoCoreError) as e:
            logger.warning("an image was not read back: %s", _first_line(e))
            raise AssetStoreUnavailable("the object storage could not read an image") from e

    def remove(self, key: str) -> None:
        """Remove what is stored under ``key``; removing nothing is not a failure."""
        try:
            self._client.delete_object(Bucket=self._bucket, Key=key)
        except (ClientError, BotoCoreError) as e:
            logger.warning("an image was not removed: %s", _first_line(e))
            raise AssetStoreUnavailable("the object storage could not remove an image") from e


def _first_line(error: Exception) -> str:
    """The service's first line names the problem; the rest may hold a request and a key."""
    lines = str(getattr(error, "orig", None) or error).strip().splitlines()
    return lines[0] if lines else type(error).__name__
