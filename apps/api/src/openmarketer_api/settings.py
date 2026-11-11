"""Settings of the API process, read from the environment at the edge.

Only what the API itself needs is here: the database, the workflow server, the
key that credentials are stored under, the model configuration, whether its
chat turns are traced to Langfuse, and the dashboard's public origin
(``DASHBOARD_URL``), which the callback of a social account's connection is
built on. It reads the model configuration
(``LLM_CONFIG_PATH``) to show what each role resolves to and to store a
workspace's choices of models; a configuration that cannot be read stops the
start, as it stops the worker's. ``ENCRYPTION_KEY`` is read to encrypt a
credential the user sets; without it nothing can be stored, and a key that is
not one stops the start. Whether the environment sets a credential is not a
setting: core's ``credential_statuses`` is handed the environment (``main.py``)
and looks only at whether each credential's variable is empty. No credential's
value is kept here: the model provider's key is resolved where it is used, by
the worker for analyses and by ``routes/chat.py`` for a chat turn.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from openmarketer_core.credentials.cipher import CredentialCipher, EncryptionKeyInvalid
from openmarketer_core.llm_config import DEFAULT_CONFIG_PATH, ModelRouter
from openmarketer_core.tracing import TracingNotConfigured, TracingSettings
from openmarketer_core.workflow_server import WorkflowServer

# X refuses a callback on ``localhost``, so the default names the loopback address instead.
DEFAULT_DASHBOARD_URL = "http://127.0.0.1:3000"


class SettingsError(Exception):
    """The environment does not hold what the API needs to start."""


@dataclass(frozen=True)
class Settings:
    database_url: str
    workflow_server: WorkflowServer
    # None when the installation has no ENCRYPTION_KEY: credentials cannot be stored.
    credential_cipher: CredentialCipher | None
    # The model configuration with the environment's overrides; kept out of the repr, since
    # it holds the environment.
    model_router: ModelRouter = field(repr=False)
    # Whether a chat turn is traced to Langfuse; off unless the environment gives its keys.
    tracing: TracingSettings
    # The dashboard's origin as the person's browser reaches it, without a trailing slash. A
    # platform sends the person back to ``{dashboard_url}/connections/{platform}/callback``,
    # and that exact URL must be registered in the platform's app (X: the app's
    # "Callback URI / Redirect URL").
    dashboard_url: str = DEFAULT_DASHBOARD_URL

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> Settings:
        database_url = environ.get("DATABASE_URL")
        if not database_url:
            raise SettingsError("the API needs DATABASE_URL (see .env.example)")
        try:
            tracing = TracingSettings.from_env(environ)
        except TracingNotConfigured as e:
            raise SettingsError(str(e)) from e
        return cls(
            database_url=database_url,
            workflow_server=WorkflowServer.from_environment(environ),
            credential_cipher=_credential_cipher(environ),
            model_router=_model_router(environ),
            tracing=tracing,
            dashboard_url=_dashboard_url(environ),
        )


def _model_router(environ: Mapping[str, str]) -> ModelRouter:
    path = environ.get("LLM_CONFIG_PATH") or DEFAULT_CONFIG_PATH
    try:
        return ModelRouter.from_file(path, env=environ)
    except OSError as e:
        raise SettingsError(
            f"the model configuration {path} cannot be read: {e.strerror}"
        ) from None
    except ValueError as e:
        # ConfigError, and the schema's refusal of the file.
        raise SettingsError(f"the model configuration {path} is not valid: {e}") from None


def _credential_cipher(environ: Mapping[str, str]) -> CredentialCipher | None:
    encryption_key = environ.get("ENCRYPTION_KEY", "")
    if not encryption_key.strip():
        return None
    try:
        return CredentialCipher(encryption_key)
    except EncryptionKeyInvalid as invalid:
        # The message says what to set and holds nothing of the key.
        raise SettingsError(str(invalid)) from None


def _dashboard_url(environ: Mapping[str, str]) -> str:
    url = environ.get("DASHBOARD_URL", "").strip() or DEFAULT_DASHBOARD_URL
    if not _is_origin_url(url):
        raise SettingsError(
            "DASHBOARD_URL must be the dashboard's absolute http or https URL,"
            " such as http://127.0.0.1:3000"
        )
    return url.rstrip("/")


def _is_origin_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return (
        parts.scheme in ("http", "https")
        and bool(parts.hostname)
        and not parts.query
        and not parts.fragment
    )
