---
name: api
description: FastAPI service of OpenMarketer in apps/api. Use for endpoints, request and response models, dependencies, error handling and the OpenAPI contract the dashboard consumes.
---

You own `apps/api` (package `openmarketer_api`). Read `AGENTS.md` first. `make api` runs `openmarketer_api.main:app_from_env`; `create_app` builds the same application without services, for tests and `make openapi`. What exists: projects (`routes/projects.py`), analyses behind the `AnalysisRuns` port with an in-process implementation (`analysis_runs.py`, `in_process_runs.py`), profile review calling `openmarketer_core.db.profile_versions` directly (`routes/profiles.py`), local-mode identity (`identity.py`) and the error table (`errors.py`). After changing a route or a model run `make openapi` and commit `apps/api/openapi.json`.

How to work
- The API is a thin layer. Domain logic lives in `packages/core`; an endpoint validates input, calls core, and maps the result. If you are writing business rules in a route, move them to core.
- Request and response bodies are Pydantic models with `extra="forbid"`. Reuse `openmarketer_core.profile.ProductProfile` for profile content instead of redefining it.
- The OpenAPI document is the contract: the dashboard generates its types from it with `openapi-typescript`. Give every route a response model and a stable operation id, and say so when a change breaks the contract.
- Every route is scoped to a workspace. Approving a profile is an authenticated user action and is recorded with who and when; no route lets a model output approve anything.
- Long work (cloning and analysing a repository) does not run inside a request. Start it and return something the client can poll.
- Errors are explicit: domain errors map to 4xx with a readable message, and nothing leaks stack traces, file system paths outside the repository, or credentials.
- Tests use FastAPI's test client with dependencies overridden; no network, no real model calls.
- Layering: this is an entry point. A route parses and authorises, calls exactly one use case from core, and maps the result or the domain error to a response. Dependencies (session, chat model, current user) are built in FastAPI dependencies and passed in; routes import no SQLAlchemy and no agent code.
- Readability: one router module per resource, request and response models next to the routes that use them, and handlers short enough to read without scrolling.

Done means `make check` passes and new routes have tests for the success path and the main failure paths.
