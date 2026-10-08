---
name: persistence
description: Database layer of OpenMarketer. Use for SQLAlchemy models, Alembic migrations, sessions, repositories that store snapshots, evidence and Product Profile versions, and workspace isolation (row-level security).
---

You own the database layer in `packages/core/src/openmarketer_core/db/`. Read `AGENTS.md` first.

What exists
- `base.py`: declarative base and constraint naming convention.
- `models.py`: `Workspace`, `Project`, `RepoSnapshot`, `Evidence`, `ProductProfileRecord` (unique per project and version; approved if and only if `approved_at` is set).
- `migrations/`: Alembic, URL from `DATABASE_URL`. Tests in `packages/core/tests/test_db.py` run against the dev PostgreSQL on port 5433; the database fixtures (`alembic_config`, `engine`, `session`) are in `conftest.py` at the repository root and give each test module its own temporary database. `test_session.py` and `test_evidence_store.py` cover the two modules below.
- `session.py`: `session_factory(database_url)`, the `transaction(sessions)` unit of work (commits when the block ends, rolls back if it raises) and `DatabaseError`. It does not read the environment; the entry point passes the URL.
- `evidence_store.py`: `save_analysis(session, workspace_id=..., snapshot=..., facts=..., profile=...)` stores one analysis run: it finds or creates the workspace's project for the repository (matched by exact `source_repo_url`), then adds a `RepoSnapshot`, one `Evidence` row per extractor fact and a draft `ProductProfileRecord` with the next version. `local_workspace_id(session)` finds or creates the workspace named `local` for single-user mode. It locks the workspace row first, so saves in one workspace run one after another and two runs cannot create the same project or take the same version. The functions flush and never commit.
- The only caller is the CLI: `openmarketer analyze <repo> --save` (tests in `apps/cli/tests/test_analyze.py`). Nothing reads the rows back yet, no code approves a profile, and there is no row-level security.

How to work
- SQLAlchemy 2 typed style (`Mapped`, `mapped_column`). Enums are non-native with a CHECK constraint; do not pass `create_constraint=True`, it duplicates the constraint.
- Change `models.py`, run `make migration m="..."`, then read the generated migration and fix it by hand. Autogenerate gets enums, CHECK constraints and pgvector columns wrong often enough that it is never committed unread.
- Every migration must upgrade and downgrade cleanly on a fresh database.
- The profile content stored in a record is a validated `ProductProfile`; review state and version numbers belong to the record, not to the schema a model writes.
- Every query is scoped to a workspace. If you add row-level security, the policy is in a migration and a test proves one workspace cannot read another's rows.
- Keep persistence free of web and agent frameworks: plain functions or small repository classes that take a session.
- Layering: this is an adapter. Use cases ask for what they need through a small repository interface and receive a session from the entry point; SQLAlchemy types do not leak into the domain, and domain models (`ProductProfile`) are converted at the boundary. No business rule lives in a model class or a query.
- Readability: one repository function per question the application asks (`latest_approved_profile(project_id)`), named for that question rather than for the SQL behind it.

Done means `make check` passes with the dev stack running, including a test for each new constraint or query.
