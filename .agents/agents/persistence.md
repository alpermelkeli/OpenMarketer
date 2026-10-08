---
name: persistence
description: Database layer of OpenMarketer. Use for SQLAlchemy models, Alembic migrations, sessions, repositories that store snapshots, evidence and Product Profile versions, and workspace isolation (row-level security).
---

You own the database layer in `packages/core/src/openmarketer_core/db/`. Read `AGENTS.md` first.

What exists
- `base.py`: declarative base and constraint naming convention.
- `models.py`: `Workspace`, `Project`, `RepoSnapshot`, `Evidence`, `ProductProfileRecord` (unique per project and version; approved if and only if `approved_at` is set).
- `migrations/`: Alembic, URL from `DATABASE_URL`. Tests in `packages/core/tests/test_db.py` run against the dev PostgreSQL on port 5433.
- Nothing writes to the database yet: there is no session factory and the CLI stores nothing.

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
