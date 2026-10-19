"""Settings of the studio's workflow, read from the environment at the edge.

What an operator can change about a studio run: how long one attempt at a step may take, how
many rounds a run may make (``STUDIO_MAX_ITERATIONS``), what one asset may cost
(``STUDIO_MAX_COST_PER_ASSET_USD``, model calls and paid images together), and the sandbox
agent-written code runs in (its image, its limits, the proxy its installs go through, a
stronger container runtime). The other limits and every default, with their reasons, are in
``policy.py``, core's ``rules.Limits`` and core's ``SandboxSettings``; this module only fills
them. It takes the environment as an argument and is called when the worker starts; the
workflow and the activities read none of it.

A value that does not fit stops the worker at start-up with the variable's name, not every
run: a number that is not one, and a sandbox whose time limits do not fit in the time a
program or a render is given (``definitions.py`` in core), which would end every one of them
as timed out. (Reading an image's text is a short run whatever the limit, and is not held to
it.)
The caps of the creative budget are not here: they are resolved for a run's workspace at each
attempt (``MEDIA_MONTHLY_CAP_USD``, ``MEDIA_AUTO_APPROVE_USD``, read by the runtime's budget).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field, replace

from openmarketer_core.studio.docker_sandbox import SandboxSettings
from openmarketer_core.studio.output_check import OutputLimits
from openmarketer_core.studio.studio_agent import rules
from openmarketer_core.studio.studio_agent.definitions import (
    PYTHON_TIMEOUT_SECONDS,
    READ_TEXT_TIMEOUT_SECONDS,
    RENDER_TIMEOUT_SECONDS,
)
from openmarketer_worker.creative_studio.policy import StudioPolicy
from openmarketer_worker.settings import SettingsError, positive_number


@dataclass(frozen=True)
class StudioSettings:
    """What the studio's worker side is configured with."""

    policy: StudioPolicy = field(default_factory=StudioPolicy)
    limits: rules.Limits = field(default_factory=rules.Limits)
    sandbox: SandboxSettings = field(default_factory=SandboxSettings)

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> StudioSettings:
        """Raises ``SettingsError``, naming the variable, for a value that does not fit."""
        policy = StudioPolicy()
        limits = rules.Limits()
        sandbox = SandboxSettings()
        settings = cls(
            policy=replace(
                policy,
                attempt_timeout_seconds=60
                * positive_number(
                    environ, "STUDIO_TIMEOUT_MINUTES", policy.attempt_timeout_seconds / 60
                ),
            ),
            limits=replace(
                limits,
                max_rounds=_whole_number(environ, "STUDIO_MAX_ITERATIONS", limits.max_rounds),
                max_cost_usd=_amount(environ, "STUDIO_MAX_COST_PER_ASSET_USD", limits.max_cost_usd),
            ),
            sandbox=replace(
                sandbox,
                image=_text(environ, "STUDIO_SANDBOX_IMAGE", sandbox.image),
                run_timeout_s=positive_number(
                    environ, "STUDIO_SANDBOX_TIMEOUT_S", sandbox.run_timeout_s
                ),
                memory_mb=_whole_number(environ, "STUDIO_SANDBOX_MEMORY_MB", sandbox.memory_mb),
                cpus=_amount(environ, "STUDIO_SANDBOX_CPUS", sandbox.cpus),
                pids=_whole_number(environ, "STUDIO_SANDBOX_PIDS", sandbox.pids),
                outputs=replace(
                    OutputLimits(),
                    max_files=_whole_number(
                        environ, "STUDIO_SANDBOX_MAX_OUTPUTS", OutputLimits().max_files
                    ),
                ),
                install_timeout_s=positive_number(
                    environ, "STUDIO_SANDBOX_INSTALL_TIMEOUT_S", sandbox.install_timeout_s
                ),
                proxy_network=_text(environ, "STUDIO_SANDBOX_PROXY_NETWORK", sandbox.proxy_network),
                proxy_url=_text(environ, "STUDIO_SANDBOX_PROXY_URL", sandbox.proxy_url),
                runtime=environ.get("STUDIO_SANDBOX_RUNTIME", "").strip() or None,
            ),
        )
        _require_a_sandbox_that_fits_its_actions(settings.sandbox)
        return settings


def _require_a_sandbox_that_fits_its_actions(sandbox: SandboxSettings) -> None:
    """Refuse limits under which a program, a render or a reading always runs out of time."""
    margin = sandbox.startup_margin_s
    fits = {
        "a program (its install and its run)": (
            sandbox.install_timeout_s + sandbox.run_timeout_s + 2 * margin,
            PYTHON_TIMEOUT_SECONDS,
        ),
        "a render": (sandbox.run_timeout_s + margin, RENDER_TIMEOUT_SECONDS),
        "a reading": (sandbox.run_timeout_s + margin, READ_TEXT_TIMEOUT_SECONDS),
    }
    for what, (needed, given) in fits.items():
        if needed > given:
            raise SettingsError(
                f"STUDIO_SANDBOX_TIMEOUT_S and STUDIO_SANDBOX_INSTALL_TIMEOUT_S let {what} take "
                f"{needed:g} seconds with the sandbox's start-up, more than the {given:g} "
                "seconds its action is given: lower them"
            )


def _whole_number(environ: Mapping[str, str], name: str, default: int) -> int:
    text = environ.get(name)
    if not text:
        return default
    number = positive_number(environ, name, default)
    if number != int(number):
        raise SettingsError(f"{name} must be a whole number, not {text!r}")
    return int(number)


def _amount(environ: Mapping[str, str], name: str, default: float) -> float:
    """A number above 0 (a fraction is fine: a cap of half a dollar, half a CPU)."""
    text = environ.get(name)
    if not text:
        return default
    try:
        amount = float(text)
    except ValueError:
        amount = 0.0
    if not math.isfinite(amount) or amount <= 0:
        raise SettingsError(f"{name} must be a number above 0, not {text!r}")
    return amount


def _text(environ: Mapping[str, str], name: str, default: str) -> str:
    return environ.get(name, "").strip() or default
