"""Keeping the pages a research run fetched in the installation's object storage.

A fetched page is stored here and never in the database: rows keep what a page
is, and the object keeps what it said. Keys are ``pages/<workspace>/<project>/
<run>/<page>.html``, so the pages of one run can be found, counted and removed
without reading a row. A report keeps only the quotes it relied on; the pages
themselves are working material, and the worker removes them after their
retention (``db/research_pages.py``).

``S3PageStore`` speaks the S3 API through boto3, so it works against MinIO in
the dev stack (which the endpoint and the credentials default to) and any other
S3-compatible service. The bucket is expected to exist; nothing here creates
one. A page is bounded before it gets here (``page_fetch.py``), and the store
adds no rule of its own: it stores bytes, answers with bytes, removes what it
is told, and words every failure as unavailable without showing what the
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

logger = logging.getLogger(__name__)

PAGES_PREFIX = "pages"
# The dev stack's MinIO, as the worker on the host reaches it; production sets its own.
DEFAULT_ENDPOINT_URL = "http://localhost:9000"
DEFAULT_BUCKET = "openmarketer-assets"
DEFAULT_REGION = "us-east-1"
DEFAULT_ACCESS_KEY = "openmarketer"
DEFAULT_SECRET_KEY = "openmarketer-dev"


class PageStoreUnavailable(Exception):
    """The object storage refused the work or could not be reached."""


class PageStore(Protocol):
    """What a research run needs of object storage: store bytes, read them back, remove them."""

    def put(self, key: str, body: bytes) -> None: ...

    def get(self, key: str) -> bytes: ...

    def remove(self, key: str) -> None: ...


def page_key(
    *, workspace_id: uuid.UUID, project_id: uuid.UUID, run_id: uuid.UUID, page_id: uuid.UUID
) -> str:
    """Where the object of one page lives: under its run, which is under its project."""
    return f"{PAGES_PREFIX}/{workspace_id}/{project_id}/{run_id}/{page_id}.html"


class S3PageStore:
    """``PageStore`` over an S3-compatible service, through boto3."""

    def __init__(self, client: Any, *, bucket: str) -> None:
        """``client`` is a boto3 S3 client (or a fake of one, in tests)."""
        self._client = client
        self._bucket = bucket

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> S3PageStore:
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

    def put(self, key: str, body: bytes) -> None:
        """Store ``body`` under ``key``, replacing what is there."""
        try:
            self._client.put_object(Bucket=self._bucket, Key=key, Body=body)
        except (ClientError, BotoCoreError) as e:
            logger.warning("a page was not stored: %s", _first_line(e))
            raise PageStoreUnavailable("the object storage could not store a page") from e

    def get(self, key: str) -> bytes:
        """The bytes stored under ``key``. Raises ``PageStoreUnavailable`` when unreadable."""
        try:
            return self._client.get_object(Bucket=self._bucket, Key=key)["Body"].read()
        except (ClientError, BotoCoreError) as e:
            logger.warning("a page was not read back: %s", _first_line(e))
            raise PageStoreUnavailable("the object storage could not read a page") from e

    def remove(self, key: str) -> None:
        """Remove what is stored under ``key``; removing nothing is not a failure."""
        try:
            self._client.delete_object(Bucket=self._bucket, Key=key)
        except (ClientError, BotoCoreError) as e:
            logger.warning("a page was not removed: %s", _first_line(e))
            raise PageStoreUnavailable("the object storage could not remove a page") from e


def _first_line(error: Exception) -> str:
    """The service's first line names the problem; the rest may hold a request and a key."""
    lines = str(getattr(error, "orig", None) or error).strip().splitlines()
    return lines[0] if lines else type(error).__name__
