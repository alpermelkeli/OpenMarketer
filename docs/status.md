# Implementation status

What is built, how it was checked, and where the code differs from the [design report](design-report/report.pdf). The report describes the whole design; this page describes the repository as it is. Last updated 2026-10-08.

## What is built

Phase 1 of the roadmap is "repository analyzer and Product Profile": extractors, secret scan, evidence store, LLM synthesis, review UI and golden-repository evaluation.

| Part | State | Where |
|---|---|---|
| Product Profile schema | Built | `packages/core/src/openmarketer_core/profile.py` |
| Intake: locked-down clone, secret scan, safe file access | Built | `packages/core/src/openmarketer_core/intake/` |
| Extractors (11, as plugins) | Built | `packages/extractors/` |
| Analyzer agent | Built, runs from the CLI | `packages/core/src/openmarketer_core/analyzer/` |
| Model configuration and chat client | Built | `config/models.yaml`, `packages/core/src/openmarketer_core/llm_config.py`, `llm.py` |
| Database models and migrations | Built | `packages/core/src/openmarketer_core/db/models.py`, `db/migrations/` |
| Evidence store: saving snapshots, evidence and profile versions | Built, used by the CLI with `--save`; see [Storing a run](#storing-a-run) for what it leaves out | `packages/core/src/openmarketer_core/db/evidence_store.py`, `db/session.py` |
| Profile versions: editing, approval, latest approved and latest draft | Built, used by the API. See [Profile versions and approval](#profile-versions-and-approval) | `packages/core/src/openmarketer_core/db/profile_versions.py` |
| Project store: creating a project and reading it inside its workspace | Built, used by the API | `packages/core/src/openmarketer_core/db/projects.py` |
| Pipeline as one operation (intake, extractors, analyzer) | Built, used by the worker; the CLI still calls the three steps itself | `packages/core/src/openmarketer_core/repository_analysis.py` |
| Analysis runs: requesting a run and recording its start and outcome | Built; the API requests runs and reads their status, the worker reports to it. See [Analysis runs](#analysis-runs) | `packages/core/src/openmarketer_core/db/analysis_runs.py`, `analysis_request.py` |
| Repository tokens: which access token may be sent to which host | Built, used by the CLI and the worker. See [Private repositories](#private-repositories) | `packages/core/src/openmarketer_core/intake/credentials.py`, `intake/git.py` |
| API | Built: projects, analyses handed to the worker, and reading, editing and approving a profile. See [The API](#the-api) | `apps/api/`, contract in `apps/api/openapi.json` |
| Worker | Built: the `AnalyzeRepository` workflow and its activities, started by the API. See [The worker](#the-worker) | `apps/worker/`, contract with the API in `packages/core/src/openmarketer_core/analysis_workflow.py` |
| Review UI | Not built (`apps/web` is a scaffold) | |
| Golden-repository evaluation | Not built | |

Three things run: the command line, end to end; the API, which registers a repository, hands its analysis to the worker and reads, edits and approves the resulting profile; and the worker, which executes the analysis as a Temporal workflow. The command line first:

```bash
make analyze repo=https://github.com/owner/name
```

It clones the repository, scans and redacts secrets, runs the extractors, lets the analyzer agent explore the files, and prints a draft Product Profile as JSON. Nothing is published, and nothing is stored unless the run is started with `save=1` (next section). The profile is a draft: no person has reviewed it.

### Storing a run

`make analyze repo=... save=1` passes `--save` to `openmarketer analyze`, which writes the run to PostgreSQL after the profile has been printed (or written with `--out`). It needs the dev stack, a migrated database, and a `DATABASE_URL` in `.env` that points at it. For the dev stack that is `postgresql+psycopg://openmarketer:openmarketer@localhost:5433/openmarketer`; the value in `.env.example` uses the host name of the compose network and does not work from the host.

```bash
make up          # start PostgreSQL (and the rest of the dev stack)
make migrate     # create the tables
make analyze repo=https://github.com/owner/name save=1
```

The database is checked before the repository is cloned, so a missing `DATABASE_URL` or an unreachable or unmigrated database ends the command before any model call is made.

The database check also creates a workspace called `local` if there is none; it stands in for a user account in single-user mode. One saved run then writes, in a single transaction:

- a `project` row in that workspace for the repository, the first time that repository is saved. It is named after the product in the profile and keeps that name if a later profile names the product differently;
- a `repo_snapshot` row: the commit and the branch that were analysed;
- one `evidence` row per extractor fact: extractor, kind, value, file and lines;
- a `product_profile` row holding the profile as JSON, with status `draft` and a version one higher than the project's highest so far. Saving the same repository again adds a snapshot and version 2; it does not replace version 1.

To look at what was stored, open a shell in the dev database with `make psql`:

```sql
-- Every stored profile version, newest first, with the commit it was drafted from
SELECT p.name, p.source_repo_url, s.commit_sha, s.ref, pp.version, pp.status, pp.created_at
FROM product_profile pp
JOIN project p ON p.id = pp.project_id
LEFT JOIN repo_snapshot s ON s.id = pp.snapshot_id
ORDER BY pp.created_at DESC;

-- The extractor facts of the most recent snapshot
SELECT extractor, kind, file, start_line, value
FROM evidence
WHERE snapshot_id = (SELECT id FROM repo_snapshot ORDER BY created_at DESC LIMIT 1)
ORDER BY extractor, kind;

-- One section of the most recent profile
SELECT jsonb_pretty(content -> 'product') FROM product_profile ORDER BY created_at DESC LIMIT 1;
```

What storing a run does not do yet:

- **No approval.** Every profile stored by a run is a draft. A version is approved through the API (`approveProfileVersion`, see [The API](#the-api)); no command or UI does it.
- **What the analyzer read is not stored.** The `evidence` table holds the extractor facts only, which the analyzer receives as hints. The evidence behind each claim in the profile (file and lines) is stored inside the profile JSON, not as rows, and the analyzer's tool calls are not stored at all.
- **A project is matched by the exact repository URL.** `https://host/a/b` and `https://host/a/b.git` become two projects, and so does the same repository analysed once from its URL and once from a local folder (stored as a `file://` URL).
- **No workspace isolation in the database.** Queries are scoped to a workspace in code; there is no row-level security (see the differences below).
- **No review UI.** The API stores drafts the same way and can read, edit and approve them, but no screen uses it yet.

If saving fails after the analysis, the profile has already been printed or written, and the command exits with an error saying it was not stored.

### Profile versions and approval

`db/profile_versions.py` holds the database side of reviewing a profile. The API's profile routes call it.

- `save_edited_profile` stores an edited profile as a new draft with the project's next version number. The version that was edited is left as it is; the new row keeps its snapshot and records it in `edited_from_id`.
- `approve_profile_version` marks one version as approved, with the approver's user id and the database's time. Approving a version a second time raises `ProfileAlreadyApproved`.
- `latest_approved_profile` and `latest_draft_profile` return the approved version, or the draft, with the highest version number, or nothing. The latest draft can be older than the latest approved version; the caller compares the numbers.

Each function takes a workspace and a project, and a project of another workspace is treated like one that does not exist. They return a `ProfileVersion` holding the validated `ProductProfile`, not a database row.

The database enforces the rules itself, so they also hold for `INSERT`, `UPDATE` and `DELETE` statements written by hand:

- a trigger on `product_profile` accepts a new row only as a draft, so a version becomes approved only by an update of a stored draft;
- the same trigger lets an update of a draft change `status`, `approved_by` and `approved_at` and nothing else: not the content, the version number, the project, the snapshot or the creation time. It compares whole rows, so a column added later is frozen too unless the trigger is changed;
- it refuses every update of an approved row, and every deletion, draft or approved. A version number is therefore never used twice, and approving "version 2" always approves the content that was stored as version 2;
- a CHECK constraint requires an approved row to have both `approved_by` and `approved_at`, and a draft to have neither.

A consequence: a project that has profile versions cannot be deleted with plain SQL, because its versions cannot be. Removing a project will need a deliberate, privileged path when that feature is built.

What this does not do:

- **No screen or command.** The API routes edit and approve a profile; no review UI or CLI command does.
- **The approver is not checked.** There is no user table; `approved_by` is whatever id the caller passes. The API passes the one local user (see [The API](#the-api)).
- **No rule about which version may be approved.** An older draft can be approved after a newer version; "latest approved" follows the version number, not the time of approval.
- **`TRUNCATE` is not stopped, and the trigger can be removed.** Row triggers do not fire for `TRUNCATE`, and the owner of the table can disable or drop the trigger. Downgrading the migration does exactly that: while a database is downgraded nothing protects its versions, and a later upgrade cannot tell whether approved content was rewritten in between.
- **An approved row without an approver stops the upgrade.** The first revision allowed one, though no code wrote it. The migration refuses rather than guess; its docstring says how to repair the row.
- **Writes are serialised only under READ COMMITTED**, PostgreSQL's default. Under a stricter isolation level a write that had to wait fails on the unique constraint instead of taking the next version number.
- **No single-version or list query.** Nothing fetches one version by number or lists a project's versions yet.

### The API

`make api` serves the API on `http://127.0.0.1:8000`. It needs the dev stack, a migrated database and `DATABASE_URL` in `.env`, like a stored run. An analysis also needs `make worker` running: the API stores the run and starts its workflow, the worker executes it. So, to analyse a repository through the API: `make up`, `make migrate`, `make worker` and `make api`. The API itself clones nothing, calls no model and reads no repository token.

The API finds Temporal through `TEMPORAL_ADDRESS` and `TEMPORAL_NAMESPACE`, read the same way as by the worker (`WorkflowServer` in `analysis_workflow.py`): empty means `localhost:7233` and `default`. It does not connect when it starts. The first analysis that is started connects, and the connection is kept; so the API starts without Temporal, and projects, run status and profile review work without it.

| Method and path | Operation id | What it does |
|---|---|---|
| `GET /health` | `getHealth` | Says the process answers. It does not check the database |
| `POST /v1/projects` | `createProject` | Registers a repository under a name. Only `https://` URLs; one project per repository URL in a workspace |
| `POST /v1/projects/{project_id}/analyses` | `startAnalysis` | Stores a `queued` run, starts its workflow and returns at once (202) with the run to poll. 409 while the project has an unfinished run; 400 for a project whose repository is not an `https://` URL; 503 `analysis_not_started` when the workflow could not be started (below). The request takes no body |
| `GET /v1/projects/{project_id}/analyses/{run_id}` | `getAnalysis` | The run as the database has it: `queued`, `running`, `succeeded` (with the stored profile version) or `failed` (with the reason the worker recorded), and when it was requested, started and finished. Temporal is not asked |
| `GET /v1/projects/{project_id}/profile/draft` | `getDraftProfile` | The draft with the highest version number; 404 when every version is approved or there is none |
| `GET /v1/projects/{project_id}/profile/approved` | `getApprovedProfile` | The approved version with the highest version number |
| `POST /v1/projects/{project_id}/profile/versions/{version}/edits` | `saveProfileEdit` | Stores an edit of that version as a new draft with the next version number. The edited version, draft or approved, is not changed |
| `POST /v1/projects/{project_id}/profile/versions/{version}/approval` | `approveProfileVersion` | Approves one version as the current user. The request takes no body, and one that is sent is a 422, so that a client sending `approved_by` learns it had no effect; a second approval is 409 |

Errors have one shape, `{"code": ..., "message": ...}`, except request validation, which is FastAPI's 422. The contract is `apps/api/openapi.json`: `make openapi` writes it without starting the server, and a test fails when the committed file no longer matches the routes. The dashboard's types are not generated from it yet.

When the workflow of a run cannot be started (Temporal unreachable within 5 seconds, or refusing), the run that was just stored is marked `failed` with the text "the analysis could not be handed to a worker", and the request is answered 503 with the code `analysis_not_started` and the run's id in the message. The failed run does not block the project: the client starts a new one. The order is fixed in `request_analysis` (`analysis_request.py`): the run is committed first, so the worker finds the row; then the workflow is started. A workflow that already exists for the run counts as started.

What the API does not do yet:

- **A profile response does not say which version an edit came from.** The database records it (`edited_from_id`); the route would need a second query to turn it into a version number, and nothing needs it yet.
- **There is no login.** The user is whoever is at this machine: a request is served only if it comes from a loopback address, is addressed to `localhost` or `127.0.0.1`, and, when a browser sent an `Origin`, that origin is local too. Everything else gets 403. The server binds to `127.0.0.1`. Do not put it behind a proxy or bind it to another address: a proxy on the same machine makes every request look local. Approvals are recorded under one fixed identifier for the local user (`LOCAL_USER_ID` in `identity.py`); it never comes from the request.
- **A run cannot be cancelled or listed through the API**, and nothing times out a run whose workflow is lost; the cases are listed under [The worker](#the-worker).
- **A start can be answered 503 although a workflow was started.** If Temporal starts the workflow but its answer does not arrive within 10 seconds, the run is marked failed; the worker then finds a finished run and does not analyse it.
- **Any https host is cloned from**, including hosts on the local network. That is acceptable while only the local user can call the API and has to be decided before it is exposed.
- **A browser on another origin is not served.** There is no CORS configuration; the dashboard has to call the API through its own server or a rewrite.
- **No routes to list or read projects, to list runs, or to list a project's profile versions or read one by number.**

### Analysis runs

The design runs an analysis in a Temporal workflow that the API starts. `db/analysis_runs.py` and the `analysis_run` table hold the state of such a run, so the API and the worker share it and it survives a restart of either. The API requests a run and reads its status; the worker reports to it.

- `request_analysis_run` adds a `queued` run for a project. A project has at most one unfinished run (a partial unique index); a second request raises `AnalysisAlreadyRunning`, so a repeated request does not pay for the model twice.
- `mark_run_started`, `mark_run_succeeded` and `mark_run_failed` are what the worker reports. A run only moves forwards: `queued` to `running` to `succeeded`, or `queued` or `running` to `failed`. A report that repeats what is already recorded changes nothing, because Temporal can deliver an activity twice. A finished run is never changed (`AnalysisRunFinished`), and a run that was never started cannot succeed (`AnalysisRunNotStarted`).
- `analysis_run` reads one run: status, times, the failure message, and for a succeeded run the profile version it stored and that version's snapshot.
- `save_analysis_of_project` in the evidence store stores a run's snapshot, evidence and draft profile under a project given by id, where the command line's `save_analysis` finds or creates the project by repository URL. The worker stores the result and reports the success in one transaction.

Every function takes the workspace, the project and the run, including those the worker calls; a run of another workspace or project is treated like one that does not exist, and a project the workspace does not have is `ProjectNotFound` from `db/projects.py`. CHECK constraints keep a row consistent: a profile version if and only if the run succeeded, an error if and only if it failed, an end time if and only if it is finished, a start time once it is running. A foreign key over both columns makes the profile version belong to the run's project.

What this does not do:

- **No workflow id is stored.** The caller derives it from the run id (`analysis_workflow_id`).
- **Nothing here times a run out.** The workflow reports the failure; the cases in which it cannot are listed under [The worker](#the-worker).
- **Runs are never deleted.** There is no clean-up and no way to cancel.
- **The failure message is stored as given**, cut to 2000 characters. What may be written there is the caller's decision; the worker's is described below.
- **A finished row is protected in code only.** Unlike profile versions there is no trigger, so SQL written by hand can rewrite a finished run within what the CHECK constraints allow.
- **The snapshot's repository is not compared with the project's** when a result is stored under a project id.
- **No list query.** Nothing lists a project's runs or returns its latest one.
- **No access token.** The token for a private repository is not stored in the database and does not pass through these functions.

### The worker

`make worker` runs a Temporal worker (`python -m openmarketer_worker`) that executes one workflow, `AnalyzeRepository`, on the task queue `openmarketer-analysis`. It needs the dev stack, a migrated database, and in `.env`: `DATABASE_URL`, `OPENROUTER_API_KEY`, and `TEMPORAL_ADDRESS` set to `localhost:7233` or left empty (the value in `.env.example`, `temporal:7233`, is the host name inside the compose network and does not resolve from the host, like the one in `DATABASE_URL`). The worker refuses to start, with the name of the setting, when one of these is missing or wrong or Temporal cannot be reached.

What the API and the worker share is in `openmarketer_core/analysis_workflow.py`, which imports nothing from Temporal: the workflow name, the task queue, `analysis_workflow_id(run_id)` and the workflow's input, `AnalyzeRepositoryInput` (workspace, project and run identifiers). Temporal keeps every input and result in the workflow history, readable in its UI, so nothing else travels through it: no repository URL, no token, no profile. The worker reads the repository URL from the database.

The workflow runs three activities:

| Activity | What it does | One attempt may take | Attempts |
|---|---|---|---|
| `start_run` | `mark_run_started`; returns the limits below, which the workflow cannot read from the environment itself | 30 s | 5, half a second apart at first. A run that is not found is tried again, because the request that created it may not have committed yet |
| `analyse_and_store` | Reads the project's URL, clones into a temporary folder, runs `analyze_repository`, then stores the result (`save_analysis_of_project`) and marks the run succeeded in one transaction | 30 min (`ANALYSIS_TIMEOUT_MINUTES`); a heartbeat every 20 s, missed for 60 s, ends the attempt | 2 (`ANALYSIS_MAX_ATTEMPTS`), the second a minute after the first |
| `record_failure` | `mark_run_failed` with the reason. Runs whenever either of the others ends without a result | 30 s | Until it works, for up to an hour |

Why these numbers: every attempt at an analysis can spend the run's whole model budget again, so there is one retry and no more. That retry is for what can pass: a worker that died or was restarted, a model provider that failed or limited the rate (HTTP 429), a database that was unreachable. A repository that cannot be cloned, a URL that is not `https://`, a profile the analyzer could not produce within its limits (`AnalysisError`) and a configuration error are not tried again. The numbers are in one module (`policy.py`).

- **A repeated activity does not repeat its work.** An attempt first reads the run: if it has succeeded, nothing is analysed or stored. If two attempts do run at once (the first was given up on while its thread was still working), both analyse, the first to finish stores its result, and the second's transaction is rolled back by the run's transition rules, so there is one profile version per run.
- **The failure message** is the error of the pipeline with every path under the temporary folder replaced by `<clone>` (`failure_message` in `repository_analysis.py`), or a fixed sentence for a timeout, a lost worker, a cancellation, a failed write or an unexpected error. The original error is written to the worker's log, not to the history. Activities attach no cause to a failure, because Temporal stores causes too.
- **The temporary clone is removed** when an attempt ends, whatever its outcome.
- **A project stored from the command line with a local folder is refused**: the worker clones `https://` URLs only.

What the worker does not do:

- **Some failures leave a run unfinished**, and an unfinished run blocks its project until the row is changed by hand:
  - the workflow is terminated (from the UI, the CLI, or by an execution timeout set by whoever started it): a terminated workflow runs no further code;
  - Temporal loses the workflow or its history (for the dev stack: `make reset`, which deletes its volume), or the API process died between committing the run and starting its workflow, or the database refused the API's attempt to mark such a run failed;
  - the database stays unreachable for the hour `record_failure` keeps trying;
  - no worker is running: the run waits, `queued` or `running`, until one is started. This is the intended behaviour and has no time limit.
- **No cancellation of the analysis itself.** Cancelling the workflow marks the run failed, but the thread that is analysing cannot be stopped: it runs until the analyzer's own limits end it, and its result is then discarded. The same holds when a worker is stopped: the process ends only after the analysis in progress returns.
- **A heartbeat shows that the worker process is alive, not that the analysis is making progress.** An analysis that hangs is ended by the attempt's time limit.
- **A failed store repeats the analysis.** The profile is not passed through the history, so when the database refuses the result the next attempt analyses again.
- **Every model provider failure is retried once**, including those that cannot pass (an invalid key, HTTP 401): `LLMError` does not carry the status.
- **No progress.** A run says `running`; it does not say which step it is at.
- **Graph checkpoints are not stored.** A retried attempt starts the analysis from the beginning.
- **A worker that is killed leaves its temporary clone on disk.**
- **The history holds the stack trace of a failure**: file names and lines of the worker's code, no values.

### Private repositories

`GITHUB_TOKEN` and `GITLAB_TOKEN` are read by the entry points (the CLI and the worker) and passed to intake as `RepositoryTokens`; `intake/credentials.py` decides which one, if any, a clone may carry.

- `GITHUB_TOKEN` is for `github.com`. `GITLAB_TOKEN` is for `gitlab.com`, or for the host named by `GITLAB_HOST` when that is set (a self-hosted GitLab; `host` or `host:port`).
- A token is offered only to an `https://` URL whose host is exactly the configured one: compared in lower case, without a trailing dot, on port 443 unless the configuration names another. There is no suffix matching (`github.com.evil.example`), and a URL with a user name, a backslash, a space or a control character gets no token.
- A repository on any other host is cloned without credentials, so public repositories work as before.
- git receives the token as a header configured for that origin only (`http.<origin>.extraHeader`), and with a token it follows no redirect (`http.followRedirects=false`). Scoping the header is not enough: after a redirect git sends its extra headers to the new host on the following requests. A test shows both, with two servers on the loopback interface.
- The token is not in the stored project URL (credentials in a URL are refused), not in a workflow or activity input, and is removed from git's error output before that becomes an error message.

Before this, the command line sent whichever of the two tokens was set to any host a URL named.

What this does not do: a private repository has not been cloned with a real token (see below); one GitLab host can be configured, not several; a token is per installation, not per project or workspace; and a repository that has moved (GitHub answers with a redirect) cannot be cloned with a token until its URL is updated.

## How it was checked

- `make check` (ruff, pyright, 578 tests) passes locally. The tests need no network: model calls are scripted and HTTP uses a fake transport. Database tests run against PostgreSQL, secret-scan tests against gitleaks, and the workflow tests of the worker and of the API's Temporal adapter against the dev stack's Temporal server; each is skipped when what it needs is not there. The CI workflow starts a Temporal dev server for them; that step has not run yet.
- Storing a run is covered by those tests: the evidence store and the session module against PostgreSQL, and `openmarketer analyze --save` with the clone, extractors and model replaced by fixed results. One complete `make analyze repo=. save=1` run, on this repository with the free model, stored a project, a snapshot, 41 evidence rows and profile version 1, and the rows were read back with psql. A second save of the same repository (version 2) has only been run in the tests.
- Profile versions and approval are covered by tests against PostgreSQL only: the functions, including two edits and two approvals running at the same time; the trigger and the CHECK constraint with hand-written SQL; and the migration applied to a database that already holds a draft, taken back one revision and applied again, and refused by a database holding an approved row without an approver. Outside the tests, one version was approved through the API on a scratch database (next item).
- The API routes are covered by those tests with FastAPI's test client, all against PostgreSQL: projects, profile review, and the analysis routes with workflows started on a fake (a run stored as queued and its workflow requested, a start that reaches no worker, a second start, the status in each state, and a run asked for under another project or workspace). The Temporal adapter is tested against the dev server: after a start, a workflow with the id derived from the run exists on the given task queue with no time limit of its own; starting it again is not an error; an unreachable address is reported. The API and the worker were run together once outside the tests, against the dev Temporal server and a scratch database, with a made-up provider key because none was available. Over HTTP: a project was created for a small public repository on github.com, an analysis was started (202, `queued`), a second start was refused (409), and the status became `running`. The API process was then stopped and a new one started: it answered the status of the same run and refused another start. The run ended `failed` with the provider's message (HTTP 401) after the worker's second attempt. A second API process pointed at an address where no Temporal listens answered a start with 503 `analysis_not_started`, showed that run as `failed`, and served the health and profile routes. An approval with a body was refused with 422. Earlier, with one draft seeded through `save_analysis`, the draft was read, an edit was saved as version 2 and approved, a second approval was refused with 409 and the approved profile was read. **No analysis started through the API has succeeded outside the tests**, for want of a model key.
- Analysis runs are covered by tests against PostgreSQL: every transition and every refusal, each CHECK constraint and the one-unfinished-run index, two requests and two reports running at the same time, and a result stored together with its success in one transaction (and not stored when the success is refused).
- The worker is covered by three sets of tests, with cloning and the model scripted. The activities run as functions against PostgreSQL: a repeated attempt, two attempts at once, a run that failed meanwhile, a run of another workspace or project, each kind of error with whether it is retried, and the clone folder gone afterwards. The workflow runs on the Temporal dev server with scripted activities: success, a failure that passes on the second attempt, attempts used up, failures that are not retried, an attempt that takes too long, a missed heartbeat, and a cancellation. The two together run on Temporal and PostgreSQL: a run ends `succeeded` with one profile version or `failed` with a message, and the decoded history is searched for the profile, the repository URL and the clone folder. The time-skipping test server is not used, because it is downloaded on first use.
- The worker was run once outside the tests, against the dev Temporal server and a scratch database, with workflows started by a script. A repository that does not exist on github.com, with a made-up `GITHUB_TOKEN`: the run ended `failed` in under a second with git's message and `<clone>` in place of the folder, after one attempt. A small public repository on bitbucket.org with a made-up provider key: it was cloned without credentials, the model call was refused (HTTP 401), the attempt was repeated a minute later and the run ended `failed` with the provider's message. In both, the history held the three identifiers, the limits and the message, and neither the token nor the folder. Stopping the worker with SIGTERM ended the process. **No run has succeeded outside the tests**: no model key was available, so a real analysis through the worker, a worker killed in the middle of one, and a clone with a real token are not checked.
- The token rule is covered by tests without network: the host comparison, what git is told, and git itself redirected from one loopback server to another.
- Intake and the extractors were run by hand on seven public repositories of different kinds (Kotlin Multiplatform, Flutter, Expo, Swift, Rust, Python, Go).
- The analyzer completed full runs against a live model on one repository ([Memoria](https://github.com/alpermelkeli/Memoria), a Kotlin Multiplatform app) with the free model named below. Three runs finished with an accepted profile at no cost; the two whose length was recorded took 12 and 19 model turns and needed no repairs.

What that does not show:

- Whether a profile is correct. A submitted profile is checked for matching the schema and for evidence that points at lines which exist. Nobody has compared a profile with a human-written one.
- Whether it works on other repositories or models. One repository and one model is an example, not a result. Two runs on the same repository produced different feature lists.
- A complete run on a paid model. Earlier attempts on paid models ended at the cost limit of $0.50, or on an account without credit, before a profile was accepted.
- Access to private repositories. Which host gets a token is tested; a clone of a real private repository with a real token is not.

## Differences from the design report

| Topic | The original design said | The code does | Why |
|---|---|---|---|
| Role of the extractors | Deterministic extractors run first and the model "only synthesises and fills gaps" | The analyzer is an agent that explores the repository itself with read-only tools (`list_files`, `search`, `read_file`). Extractor output is given to it as hints. | Repositories are too varied, and too often monorepos, for extractors to carry the understanding. Covering each stack deterministically did not scale. |
| Files with secrets | A secret scanner runs, and `.env*`, key files and build output are excluded. What happens to a finding inside an ordinary file is not specified; the first implementation excluded the whole file. | The secret is replaced by `[REDACTED]` in place and the file is scanned again; a file is excluded only if a finding cannot be redacted. `.env*`, key files and build output are still excluded outright. | Excluding whole files removed configuration and source files the analyzer needs. |
| Product type and platforms | A fixed list (`consumer_app`, `b2b_saas`, …; `ios`, `android`, …) | Open vocabularies: any lowercase slug is valid, and the listed values are only the well-known ones | A product can be anything: a browser extension, a watch app, a hardware device. |
| Workspace isolation | PostgreSQL row-level security binds a session to one project | Tables carry the identifiers; no row-level security policy exists | Not done yet. |
| Graph checkpoints | Stored in PostgreSQL so a graph can resume | The analyzer graph runs in memory; an interrupted run, or a retried attempt in the worker, starts again | Not done yet. |
| Where the analyzer runs | Inside a Temporal activity | In two places. In the worker, inside the `analyse_and_store` activity of the `AnalyzeRepository` workflow that the API starts, with run state in the `analysis_run` table, one retry, timeouts and a heartbeat; but without stored checkpoints, progress, or a way to stop an analysis that is running. And synchronously inside the CLI | The CLI is the direct way to try the analyzer; it needs no Temporal. |
| Login | OIDC, or single-user local mode | Local mode only: no user table, requests are accepted from the same machine only, approvals carry one fixed local user identifier | Not done yet. The current user and workspace are one dependency each. |
| Release tags | Tags and release notes are a source of content | Clones are shallow and fetch no tags | Not needed for the profile; needed later for the repository watcher. |
| Model configuration loader | `config/llm_config.py` | `packages/core/src/openmarketer_core/llm_config.py`; the file in `config/` re-exports it | The core package needs to import it. |
| Extractor list | `flutter, react_native, swift, kotlin, nextjs` | Extractors follow file formats, not frameworks: `package.json`, Gradle, Android manifest, `Info.plist`, Xcode project, Flutter, Expo, Cargo, `pyproject.toml`, `go.mod`, and a generic README and licence reader | Follows from extractors being hints. |

The first three are decisions, and the report text has been updated to match them. The rest are parts of the design that are not built yet. The pipeline diagram in the report (`fig_profile`) still shows the original order and has not been redrawn.

## Model for the analyzer

The `repo_analyzer` role uses the fast tier, and the fast tier is the free model `apodex/apodex-1.1-mini:free`. This is a design decision, described in the report's section on model configuration: analysing a repository costs nothing, and the model's output is checked in code and reviewed by a person. The first version of the design had the role on the strong tier. The quality of profiles from the free model has not been measured; `LLM_MODEL__REPO_ANALYZER` moves the role to another model.

## Model behaviour worth knowing

Weak or free models produced these failures against the analyzer; each is handled in code and covered by a test:

- Nested sections of the profile sent as JSON text instead of objects. They are decoded before validation.
- `confidence` left out, or the schema default `0` copied back. The tool schema shown to the model now requires `confidence` and shows no default, and a submission with a missing confidence, or `0` next to cited evidence, is rejected.
- The same rejected submission sent again. After three submissions, or when the run is out of steps or budget, the profile is repaired in code (bad evidence removed, unevidenced features set to `unknown`) and the corrections are reported. A submission that still does not match the schema at that point ends the run with an error.

Other things observed:

- A request names fallback models. When the primary model fails, OpenRouter serves the next one, which may be paid. The reply states which model answered and the CLI prints it.
- Free models are rate limited upstream (HTTP 429). Runs are best started one at a time.
- Prompt caching is not used.

## What comes next

Saving results to the database is done, and the API can create a project, have the worker analyse it as a workflow, and read, edit and approve its profile. Next, in order: the review UI (with types generated from `apps/api/openapi.json`), the golden-repository evaluation. Whether a proposed strategy belongs to Phase 1 is open: the roadmap caption mentions it, the phase description does not.
