"""Write the OpenAPI document to a file without starting the server.

The dashboard generates its types from ``apps/api/openapi.json``, which is
committed; a test fails when it no longer matches the routes.

    python -m openmarketer_api.openapi
"""

from __future__ import annotations

import json
from pathlib import Path

from openmarketer_api.main import create_app

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "openapi.json"


def render_schema() -> str:
    return json.dumps(create_app().openapi(), indent=2, sort_keys=True) + "\n"


if __name__ == "__main__":
    SCHEMA_PATH.write_text(render_schema())
    print(f"wrote {SCHEMA_PATH}")
