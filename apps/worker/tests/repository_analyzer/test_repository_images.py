"""Tests for keeping the product's own images as an analysis stores its profile.

The analyzer is wired as the worker wires it, against PostgreSQL, with the clone scripted to
hold a few images and object storage kept in memory (``conftest.py`` of the worker's tests).
"""

import io
import random
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import func, select
from temporalio.exceptions import ApplicationError

from openmarketer_core.db.analysis_runs import request_analysis_run
from openmarketer_core.db.models import AnalysisRunStatus, RepositoryImageRecord
from openmarketer_core.db.repository_images import list_repository_images
from openmarketer_core.db.session import transaction
from openmarketer_core.repository_analysis.intake import IntakeResult, RepositoryTokens
from openmarketer_core.studio.asset_store import AssetStoreUnavailable
from openmarketer_core.studio.sandbox import ImageMime

IMAGES = {
    "assets/logo.png": (512, 512),
    "screenshots/home.png": (640, 1136),
    "node_modules/pkg/logo.png": (512, 512),
}


def noise_png(width: int, height: int) -> bytes:
    image = Image.frombytes("RGB", (width, height), random.randbytes(width * height * 3))
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


@pytest.fixture
def intake_with_images(scripted_intake):
    """An intake whose clone holds ``IMAGES``, the same bytes in every clone."""
    drawn = {path: noise_png(*size) for path, size in IMAGES.items()}

    class WithImages(scripted_intake):
        def __call__(
            self, source: str, clone_into: Path, commit: str | None, tokens: RepositoryTokens
        ) -> IntakeResult:
            result = super().__call__(source, clone_into, commit, tokens)
            for path, data in drawn.items():
                (clone_into / path).parent.mkdir(parents=True, exist_ok=True)
                (clone_into / path).write_bytes(data)
            return result

    return WithImages


@dataclass
class MemoryAssets:
    """``AssetStore`` in a dictionary, counting what it was asked to store."""

    objects: dict[str, bytes] = field(default_factory=dict)
    unavailable: bool = False
    puts: int = 0

    def put(self, key: str, body: bytes, *, content_type: ImageMime) -> None:
        self._check()
        self.puts += 1
        self.objects[key] = body

    def get(self, key: str) -> bytes:
        self._check()
        return self.objects[key]

    def remove(self, key: str) -> None:
        self._check()
        self.objects.pop(key, None)

    def _check(self) -> None:
        if self.unavailable:
            raise AssetStoreUnavailable("the object storage could not be reached")


def kept_paths(database, run) -> list[str]:
    with transaction(database.sessions) as session:
        page = list_repository_images(
            session, workspace_id=run.workspace_id, project_id=run.project_id, limit=50
        )
    return sorted(image.path for image in page.items)


def rows(database, run) -> int:
    """How many images the run's project keeps, of any of its runs."""
    with transaction(database.sessions) as session:
        count = session.scalar(
            select(func.count()).where(RepositoryImageRecord.project_id == run.project_id)
        )
    return count or 0


async def test_an_analysis_keeps_the_products_images(
    analyzer_with, intake_with_images, started, database
):
    assets = MemoryAssets()
    analyzer = analyzer_with(intake=intake_with_images(), repository_images=assets)

    await analyzer.to_the_end(started)

    assert kept_paths(database, started) == ["assets/logo.png", "screenshots/home.png"]
    assert len(assets.objects) == 2
    assert all(key.startswith(f"repo-images/{started.workspace_id}/") for key in assets.objects)


async def test_an_attempt_tried_again_keeps_the_images_once(
    analyzer_with, intake_with_images, started, database
):
    assets = MemoryAssets()
    # The first attempt keeps the images, then fails to store the profile.
    unstorable = analyzer_with(intake=intake_with_images(ref="main\x00"), repository_images=assets)
    cloned = await unstorable.cloned(started)
    with pytest.raises(ApplicationError):
        await unstorable.step(started, cloned)
    puts_of_the_first = assets.puts
    assert (rows(database, started), puts_of_the_first) == (2, 2)

    again = analyzer_with(intake=intake_with_images(), repository_images=assets)
    await again.step(started, cloned)

    assert database.run(started).status is AnalysisRunStatus.SUCCEEDED
    assert (rows(database, started), assets.puts) == (2, puts_of_the_first)


async def test_unavailable_storage_leaves_the_analysis_to_succeed_without_images(
    analyzer_with, intake_with_images, started, database
):
    analyzer = analyzer_with(
        intake=intake_with_images(), repository_images=MemoryAssets(unavailable=True)
    )

    await analyzer.to_the_end(started)

    assert database.run(started).status is AnalysisRunStatus.SUCCEEDED
    assert rows(database, started) == 0


async def test_a_later_analysis_replaces_the_images_and_their_objects(
    analyzer_with, intake_with_images, database
):
    assets = MemoryAssets()
    analyzer = analyzer_with(intake=intake_with_images(), repository_images=assets)
    first = database.requested_run()
    analyzer.start(first)
    await analyzer.to_the_end(first)
    first_keys = set(assets.objects)

    with transaction(database.sessions) as session:
        second_id = request_analysis_run(
            session, workspace_id=first.workspace_id, project_id=first.project_id
        ).id
    second = replace(first, run_id=second_id)
    analyzer.start(second)
    await analyzer.to_the_end(second)

    assert rows(database, second) == 2
    assert not first_keys & set(assets.objects)
    assert len(assets.objects) == 2


async def test_without_an_image_store_no_image_is_kept(
    analyzer_with, intake_with_images, started, database
):
    await analyzer_with(intake=intake_with_images()).to_the_end(started)
    assert rows(database, started) == 0
