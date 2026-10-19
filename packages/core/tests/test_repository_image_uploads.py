"""Tests for a person's upload to a project's repository images: the rule, then storing it.

The storing tests need PostgreSQL (see the root ``conftest.py``); object storage is a fake.
"""

import io
import random
import uuid

import pytest
from PIL import Image
from sqlalchemy.orm import Session

from openmarketer_core.db.models import Project, Workspace
from openmarketer_core.db.projects import ProjectNotFound
from openmarketer_core.db.repository_images import TooManyPersonImages, list_repository_images
from openmarketer_core.repository_analysis.repository_images import uploads
from openmarketer_core.repository_analysis.repository_images.curation import (
    add_repository_image,
)
from openmarketer_core.repository_analysis.repository_images.preparation import ImageRefused
from openmarketer_core.repository_analysis.repository_images.selection import (
    RepositoryImageKind,
)
from openmarketer_core.repository_analysis.repository_images.uploads import (
    MAX_UPLOAD_BYTES,
    UploadTooLarge,
    prepare_upload,
    upload_name,
)

SHOT = RepositoryImageKind.SCREENSHOT
PERSON = uuid.uuid4()


def jpeg_with_exif(width: int = 300, height: int = 200, *, seed: int = 0) -> bytes:
    pixels = random.Random(seed).randbytes(width * height * 3)
    image = Image.frombytes("RGB", (width, height), pixels)
    exif = Image.Exif()
    exif[0x010E] = "secret description"  # ImageDescription
    out = io.BytesIO()
    image.save(out, format="JPEG", exif=exif, quality=95)
    return out.getvalue()


class FakeAssets:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put(self, key: str, body: bytes, *, content_type: str) -> None:
        self.objects[key] = body

    def get(self, key: str) -> bytes:
        return self.objects[key]

    def remove(self, key: str) -> None:
        self.objects.pop(key, None)


# ----------------------------------------------------------------------- the rule
def test_an_upload_is_written_again_without_its_metadata():
    prepared = prepare_upload("shot.jpg", jpeg_with_exif(), SHOT)
    assert b"secret description" not in prepared.data
    assert (prepared.mime, prepared.width, prepared.height) == ("image/webp", 300, 200)


def test_an_upload_that_is_not_an_image_is_refused():
    with pytest.raises(ImageRefused, match="not a PNG, JPEG or WebP"):
        prepare_upload("notes.png", b"#!/bin/sh\necho hello\n" * 200, SHOT)


def test_an_upload_too_small_to_use_is_refused():
    with pytest.raises(ImageRefused, match="short side"):
        prepare_upload("tiny.jpg", jpeg_with_exif(64, 64), SHOT)


def test_an_upload_over_the_size_limit_is_refused_undecoded():
    with pytest.raises(UploadTooLarge):
        prepare_upload("big.png", b"\x89PNG\r\n\x1a\n" + bytes(MAX_UPLOAD_BYTES), SHOT)


@pytest.mark.parametrize(
    ("given", "kept"),
    [
        ("Screen Shot 1.png", "Screen Shot 1.png"),
        ("../../etc/passwd", "passwd"),
        ("C:\\Users\\me\\logo.png", "logo.png"),
        ("ünïcödé.png", "unicode.png"),
        ("<script>.png", "script_.png"),
        ("...", uploads.UNNAMED),
        ("a" * 500 + ".png", "a" * uploads.MAX_UPLOAD_NAME_LENGTH),
    ],
)
def test_an_uploads_name_is_cleaned_and_bounded(given, kept):
    assert upload_name(given) == kept


# ----------------------------------------------------------------------- storing it
@pytest.fixture
def project(session: Session) -> dict[str, uuid.UUID]:
    workspace = Workspace(name="Acme")
    session.add(workspace)
    session.flush()
    found = Project(workspace_id=workspace.id, name="App", source_repo_url="https://example.com/a")
    session.add(found)
    session.flush()
    return {"workspace_id": workspace.id, "project_id": found.id}


def add(session, assets, project, data: bytes, *, name: str = "shot.jpg"):
    return add_repository_image(
        session, assets, **project, name=name, data=data, kind=SHOT, added_by=PERSON
    )


def test_an_upload_is_stored_cleaned_under_the_projects_person_folder(session, project):
    assets = FakeAssets()

    image = add(session, assets, project, jpeg_with_exif())

    folder = f"repo-images/{project['workspace_id']}/{project['project_id']}/person/"
    assert image.storage_key.startswith(folder)
    assert b"secret description" not in assets.objects[image.storage_key]
    assert list_repository_images(session, **project, limit=5).items == (image,)


def test_the_same_upload_again_stores_no_second_object(session, project):
    assets = FakeAssets()
    first = add(session, assets, project, jpeg_with_exif())

    again = add(session, assets, project, jpeg_with_exif(), name="again.jpg")

    assert again == first
    assert list(assets.objects) == [first.storage_key]


def test_an_upload_past_the_projects_limit_leaves_no_object(session, project, monkeypatch):
    monkeypatch.setattr(
        "openmarketer_core.repository_analysis.repository_images.curation.MAX_PERSON_IMAGES", 1
    )
    assets = FakeAssets()
    first = add(session, assets, project, jpeg_with_exif(seed=1))

    with pytest.raises(TooManyPersonImages):
        add(session, assets, project, jpeg_with_exif(seed=2))
    assert list(assets.objects) == [first.storage_key]


def test_an_upload_to_a_missing_project_leaves_no_object(session, project):
    assets = FakeAssets()
    with pytest.raises(ProjectNotFound):
        add_repository_image(
            session,
            assets,
            workspace_id=project["workspace_id"],
            project_id=uuid.uuid4(),
            name="shot.jpg",
            data=jpeg_with_exif(),
            kind=SHOT,
            added_by=PERSON,
        )
    assert assets.objects == {}
