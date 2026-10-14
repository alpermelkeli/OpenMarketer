---
name: api
description: FastAPI service of OpenMarketer in apps/api. Use for endpoints, request and response models, dependencies, error handling and the OpenAPI contract the dashboard consumes.
---

You own `apps/api` (package `openmarketer_api`). Read `AGENTS.md` first, and "The API" in `docs/status.md` for the routes and their limits. `make api` runs `openmarketer_api.main:app_from_env`; `create_app` builds the same application without services, for tests and `make openapi`.

Rules
- The API is a thin entry point: a route parses and authorises, calls one use case or store function from core, and maps the result or the domain error to a response. Routes import no SQLAlchemy and no agent code; dependencies are built in `dependencies.py`.
- The API clones nothing, calls no model and reads no token. Long work is started and polled: the start route calls `repository_analysis/request.py` (`request_analysis`), which commits the run first and then starts the workflow.
- `temporal_workflows.py` is the only module that imports `temporalio`, behind the port `Workflows` (`workflows.py`); it connects on first use and starts any workflow it is given as a `WorkflowStart` (core's `workflow_start.py`). Which workflow a run is (name, task queue, id, input) is the run's contract in core: for an analysis, `analysis_workflow` in `workflow_contract.py`, with `AnalyzeRepositoryInput` (three identifiers) as the input and nothing else, because the input is kept in Temporal's history. A new kind of run adds a function like `analysis_workflow` to its own contract and nothing to the adapter; what holds for every workflow (an id used once, no execution timeout) stays in the adapter. The API never imports the worker or the agent runtime.
- `apps/api/openapi.json` is the contract the dashboard's types come from. Every route has a response model and a stable operation id. After changing a route or a model run `make openapi` and commit the result; say so when a change breaks the contract.
- Bodies are Pydantic models with `extra="forbid"`; reuse `openmarketer_core.profile.ProductProfile` for profile content. A route that takes no body refuses one.
- Every route is scoped to a workspace, and a project of another workspace is answered exactly like a missing one. No response carries a row id of a version or a snapshot.
- Approval is recorded with who and when, from `identity.py`, never from the request; no route lets a model output approve anything.
- Errors have one shape (`Problem`: `code`, `message`; `existing_project_id` only on the 409 for an existing repository, left out otherwise). Nothing leaks a stack trace, a path or a credential.
- A list is paged like the three there are (`pagination.py`): `limit`, an opaque `cursor`, `next_cursor`; the store does the keyset query, and a cursor the API did not write is a 422.
- A route whose request holds a secret is on a router with `route_class=SecretBodyRoute` (`secret_bodies.py`), takes the secret as a `SecretStr`, and puts no length or pattern on it that Pydantic would report with the value: the rule is core's, answered as a 422 that names the rule. No response model has a field for a value or anything derived from one (`test_openapi.py` checks the contract), and a path parameter that could be mistaken for a secret has a fixed form. See "Stored credentials" in `docs/status.md`.
- The API encrypts a credential and never decrypts one: it does not call `credential_value` or `CredentialCipher.open`. `ENCRYPTION_KEY` is optional at start-up, and one that is not a key stops the start.
- Tests use FastAPI's test client with dependencies overridden, against PostgreSQL; no network, no model.

Done means `make check` passes and new routes have tests for the success path and the main failure paths.
