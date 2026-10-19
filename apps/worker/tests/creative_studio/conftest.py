"""Fixtures of the studio's tests: its fakes, its runs, and its work wired as the worker wires it.

Handlers and activities are tested as plain functions against PostgreSQL; the workflow on the
dev stack's Temporal server, on the test's own task queue. Both are skipped when they are not
reachable. The models, the image provider, the sandbox and object storage are fakes
(``studio_fakes.py``): no test needs a model, a container engine or a network.
"""

import sys
import uuid
from collections.abc import Callable, Mapping
from decimal import Decimal
from pathlib import Path

import pytest

from openmarketer_core.studio.studio_agent import rules
from openmarketer_worker import AgentRegistration
from openmarketer_worker.creative_studio import wiring
from openmarketer_worker.creative_studio.policy import StudioPolicy
from openmarketer_worker.creative_studio.studio import StudioWork

# The fakes are a module beside the tests, which are not a package.
sys.path.insert(0, str(Path(__file__).parent))

from studio_fakes import (  # noqa: E402
    CAPS,
    ESTIMATE,
    QUICK,
    FakeMedia,
    FakeSandbox,
    MemoryAssets,
    ScriptedChat,
    StudioDatabase,
    one_round_that_passes,
)


@pytest.fixture
def media() -> FakeMedia:
    return FakeMedia()


@pytest.fixture
def sandbox() -> FakeSandbox:
    return FakeSandbox()


@pytest.fixture
def assets() -> MemoryAssets:
    return MemoryAssets()


@pytest.fixture
def studio_database(sessions) -> StudioDatabase:
    return StudioDatabase(sessions)


@pytest.fixture
def studio_parts(sessions, checkpoints, media, sandbox, assets):
    """The studio's parts with the fakes in place, and what a test changes of them."""

    def parts(
        *,
        model: ScriptedChat | None = None,
        new_model: Callable[[uuid.UUID], object] | None = None,
        new_media: Callable[[uuid.UUID], object] | None = None,
        environ: Mapping[str, str] = CAPS,
        policy: StudioPolicy = QUICK,
        limits: rules.Limits | None = None,
        image_estimate: Callable[[uuid.UUID], Decimal] = lambda workspace_id: ESTIMATE,
    ) -> wiring.StudioParts:
        chat = model or one_round_that_passes()
        return wiring.StudioParts(
            sessions=sessions,
            checkpoints=checkpoints,
            new_model=new_model or (lambda workspace_id: chat),  # type: ignore[arg-type]
            new_media=new_media or (lambda workspace_id: media),  # type: ignore[arg-type]
            image_estimate=image_estimate,
            sandbox=sandbox,
            assets=assets,
            policy=policy,
            limits=limits or rules.Limits(),
            environ=environ,
        )

    return parts


@pytest.fixture
def work(studio_parts) -> StudioWork:
    """The studio's work as the wiring builds it, for its handlers as plain functions."""
    parts = studio_parts()
    return StudioWork(
        parts.sessions,
        parts.checkpoints,
        new_model=parts.new_model,
        new_media=parts.new_media,
        image_estimate=parts.image_estimate,
        limits=parts.limits,
        sandbox=parts.sandbox,
        assets=parts.assets,
    )


@pytest.fixture
def studio_registration(studio_parts) -> Callable[..., AgentRegistration]:
    def registration(**changes) -> AgentRegistration:
        return wiring.registration(studio_parts(**changes))

    return registration
