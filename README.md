# PatchShuttle

PatchShuttle is intended to provide local, auditable patch workflows for
step-by-step software development with ChatGPT and other AI services. An AI
describes a small job, the user reviews and runs it locally, and PatchShuttle
records the result for the next iteration.

> [!IMPORTANT]
> PatchShuttle is alpha software. The current `0.1.0a2` release candidate
> implements the complete local v0.1 workflow. It executes bounded read-only audits,
> approved patch transactions, and approved one-pass verification jobs under a
> project lock. Patch jobs retain backups, run controlled checks, apply scoped
> isort then Black, repeat checks, and compare SHA-256 workspace inventories.
> Completed patches support guarded manual rollback. Timestamped fixed-section
> logs, exact job archives, registry idempotency, project snapshots, AI
> handoffs, declarative Python constructors, release checks, and Trusted
> Publishing workflows are implemented. Local qualification and the required
> GitHub-hosted Windows/Ubuntu matrix are complete. TestPyPI installation
> remains the next external release gate.

## Design goals

- Keep every change local and explicitly initiated by the user.
- Make each run reviewable, reproducible, and easy to return to an AI as a log.
- Centralize recurring safety checks, backups, tests, isort, and Black.
- Support both existing repositories and projects built from an empty folder.
- Use the same typed job model from YAML, the CLI, and the Python API.

PatchShuttle is not a security sandbox. Project tests and AI-generated project
code can execute arbitrary behavior with the current user's permissions.

## Install the development build

PatchShuttle has not been published to PyPI yet. From a local checkout:

```bash
python -m venv .venv
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

Verify the installed CLI:

```bash
patchshuttle version
patchshuttle --help
```

## Initialize a workspace

Run this inside an existing project:

```bash
patchshuttle init
```

For a new project, start in an empty directory:

```bash
patchshuttle init --new-project
```

`--new-project` also accepts a real `.git` directory and the recognized regular
metadata files `.DS_Store`, `Thumbs.db`, `desktop.ini`, and AppleDouble `._*`
files. Other entries are rejected. Managed paths that are symbolic links or
have an unexpected file type are also rejected.

Initialization creates a stable random project ID and the local `patches/`
workspace containing:

- `inbox/`, `applied/`, `failed/`, `logs/`, and `backups/`;
- `state/registry.json` and `state/run.lock`;
- `patchshuttle.toml` with local policy and limits;
- `AI_GUIDE.md`, `PATCHSHUTTLE_PROTOCOL.md`, and the JSON Schema;
- valid audit and patch examples in `examples/`.

Running `init` again preserves every existing file. If a generated entry is
missing, only that missing entry is created.

The same operation is available from Python:

```python
from patchshuttle import init_workspace

result = init_workspace(".", new_project=False)
print(result.status.value, result.workspace.project_id)
```

## Validate a job

Validate the generated audit example or an AI job placed in `patches/inbox/`:

```bash
patchshuttle validate patches/examples/AUDIT-EXAMPLE.psh.yaml
patchshuttle validate patches/inbox/AUDIT-001.psh.yaml
```

A valid job produces a compact summary and exits with code `0`:

```text
VALID
job_id: AUDIT-001
kind: audit
protocol: 1
project_id: PSH-8F41C2A73D905E61
actions: 1
checks: 0
```

An invalid job is reported to standard error with a stable error code, field
path, and line and column when available:

```text
INVALID [JOB_SCHEMA_INVALID] $.protocol: Input should be 1
```

YAML and schema errors exit with code `2`. Missing workspace, invalid local
configuration, and project ID mismatch errors exit with code `3`.

The current `validate` command reads the nearest workspace configuration,
applies its `max_job_bytes` limit, safely loads the YAML, validates the typed
model, and compares the job project ID. It does not plan or execute actions,
modify project files, or create logs.

The public constructors validate declarative Python data without reading or
modifying project files:

```python
from patchshuttle import Job
from patchshuttle.actions import create_file
from patchshuttle.checks import compileall

job = Job(
    protocol=1,
    project_id="PSH-8F41C2A73D905E61",
    id="PATCH-001",
    kind="patch",
    actions=[
        create_file(path="src/example.py", content="VALUE = 1\n")
    ],
    checks=[compileall(paths=["src"])],
)

schema = Job.model_json_schema()
```

The models reject unknown fields, enforce protocol and identifier formats, and
keep validated data immutable. They do not execute the requested operations.

Load a job from disk with the same validation contract:

```python
from patchshuttle import JobError, load_job

try:
    job = load_job("patches/inbox/PATCH-001.psh.yaml")
except JobError as error:
    print(error.code.value, error.field_path, error.line, error.column)
```

The loader accepts the exact `.psh.yaml` extension and UTF-8 input, checks its
`max_bytes` limit before parsing, uses PyYAML's safe loader, and rejects
duplicate mapping keys, custom tags, anchors, aliases, unknown fields, and
invalid typed-model data. Errors expose a stable code and a YAML field path;
lexical errors also include a line and column when available.

## Plan a job

Plan a validated audit, patch, or verify job without changing the workspace:

```bash
patchshuttle plan patches/examples/PATCH-EXAMPLE.psh.yaml
```

The command displays the normalized job hash, sequential action dispositions,
files and directories that would be created or modified, requested checks,
Python formatting scope, protected-path result, backup destination template,
rollback policy, and whether later execution will require confirmation.

Planning performs the complete implemented read-only preflight:

- applies local action-count, check, size, ignored-path, and protected-path
  policy;
- simulates sequential changes in memory, including changes to a file created
  earlier in the same job;
- checks exact occurrence counts and idempotent `NO_CHANGE` states;
- parses and dry-runs text-only unified diffs without a shell or external
  `patch` command;
- rejects binary content, unsupported target encodings, mixed newline styles,
  symbolic links, special files, and file-size violations;
- validates check paths, the conservative pytest argument allowlist, dotted
  Django labels, local profiles, and required Python modules;
- records target fingerprints and computes final bytes and SHA-256 hashes for
  the internal transactional runner.

Policy blocks exit with code `4`, planning failures with code `5`, and missing
check profiles or dependencies with code `9`. A successful plan exits with
code `0`.

The same planner is available from Python:

```python
from patchshuttle import discover_workspace, load_job, plan_job

workspace = discover_workspace(".")
job = load_job(
    "patches/inbox/PATCH-001.psh.yaml",
    max_bytes=workspace.config.execution.max_job_bytes,
)
plan = plan_job(job, workspace)

print(plan.job_hash)
print(plan.files_to_create)
print(plan.files_to_modify)
```

`Plan`, its actions, checks, fingerprints, and final file changes are immutable.
Planning does not create target directories or files, execute audits or checks,
run formatters, create backups or logs, or alter registry state. A successful
plan therefore does not mean that the job was applied.

## Execute jobs

`run` is the universal executor for `audit`, `patch`, and `verify` jobs:

```bash
patchshuttle run patches/inbox/PATCH-001.psh.yaml
```

Audit jobs are read-only and do not prompt. Patch and verify jobs print the
complete plan and local-code warning before asking `Apply this job? [y/N]`.
Explicit automation can use:

```bash
patchshuttle run patches/inbox/PATCH-001.psh.yaml --yes
```

For a patch only, the user may deliberately retain partial declared changes
after a failure:

```bash
patchshuttle run patches/inbox/PATCH-001.psh.yaml --keep-changes
```

This mode requires a second deny-by-default confirmation unless combined with
`--yes`. It is rejected when local policy sets `allow_keep_changes = false`.
The log and registry distinguish `SKIPPED_CHANGES_KEPT` from a skipped rollback
where no declared change was published. Tests and profiles can still create
external side effects outside PatchShuttle's transaction.

Kind-specific CLI entry points are also available:

```bash
patchshuttle audit patches/inbox/AUDIT-001.psh.yaml
patchshuttle verify patches/inbox/VERIFY-001.psh.yaml
patchshuttle verify patches/inbox/VERIFY-001.psh.yaml --yes
```

From Python, approval is required for patch and verify plans but not audits:

```python
from patchshuttle import execute_plan

result = execute_plan(plan, approved=True)
print(result.status.value, result.backup_path, result.log_path)
```

`RunResult` is immutable and reports created and modified paths, backup and log
locations, the archived job copy, initial check results, formatter results,
retained formatted-file states, final check results, audit observations, and a
workspace comparison where applicable.

An audit executes `tree`, `read`, literal `search`, `find_files`, `file_info`,
SHA-256 `hash`, `git_status`, and `environment`. Traversal, file reads, match
counts, and recorded output are bounded by local policy. Protected and ignored
paths are skipped, source content appears only when explicitly requested by a
`read` or `search` action, and a before/after inventory verifies that the audit
did not modify the workspace.

A verify job runs its controlled checks once, does not create a backup or run
formatters, and compares the workspace before and after. A successful check
that changes a non-ignored path returns `UNEXPECTED_WORKSPACE_CHANGE`; because
project checks can have external effects, PatchShuttle reports but does not
automatically undo those changes.

A patch plan may contain `create_directory`, `create_file`, `replace_exact`,
`insert_before`, `insert_after`, `delete_exact`, or `apply_diff` actions and
applies the final bytes already computed by the planner.

For a supported plan, the implemented sequence is:

1. validate the workspace lock file and acquire a non-blocking cross-platform
   lock;
2. run the complete planner again under the lock and compare the resulting
   immutable plan with the approved plan;
3. capture a deterministic inventory of non-ignored workspace entries before
   the first project write, hashing regular files within the configured entry
   and total-byte limits;
4. copy every existing target into
   `patches/backups/<JOB_ID>/<RUN_TIMESTAMP>/originals/` before the first
   project write and create a manifest containing `PRESENT` or `ABSENT`
   entries, SHA-256 hashes, sizes, modes, encoding, and newline metadata;
5. create planned directories and files without accepting an existing-path
   race;
6. stage modified-file bytes beside the target, flush them, recheck the
   approved original fingerprint, atomically replace the target, preserve its
   mode, and verify exact final bytes and hash;
7. build fixed argument arrays for every requested check and run them in order
   from the workspace root with `shell=False`, per-check timeout, separate
   stdout and stderr capture, and local output truncation limits;
8. stop on the first failed, timed-out, or unstartable initial check and verify
   that the checks did not change any declared transaction file;
9. capture the approved changed-Python scope, then run isort followed by Black
   only on those exact relative paths using the current interpreter in isolated
   mode, fixed argument arrays, `shell=False`, and the same timeout and bounded
   output controls;
10. stop on formatter failure, reject changes to declared non-Python files,
   reject oversized or non-regular formatter targets, and retain the formatted
   bytes, SHA-256 hashes, sizes, and modes;
11. when configured, repeat the same checks and require every formatted and
    non-formatted transaction file to retain its exact approved post-state;
12. capture the final bounded inventory and classify added, removed, modified,
    and type-changed paths as declared or unexpected;
13. mark the manifest `COMPLETED`, or restore modified originals and remove
    only paths created by this attempt before recording `ROLLED_BACK` or
    `ROLLBACK_FAILED`; an explicitly accepted `--keep-changes` run instead
    records `CHANGES_KEPT` when a failed job published declared changes;
14. archive the exact CLI source job, write a fixed-section UTF-8 log with a
   compact AI handoff block, and atomically commit registry state before
   releasing the workspace lock.

A no-change plan is revalidated under the lock and returns without creating a
backup or launching checks. A race detected before replacement is not
overwritten. Rollback validates retained originals before restoring them,
refuses to follow symbolic links or remove foreign non-empty directories, and
does not claim success when a tracked path cannot be restored.

The internal check runner supports `compileall`, `pytest`, `unittest`, Django
checks and tests, validated module imports, and locally configured profiles. It
inherits the current process environment because project checks execute project
code and PatchShuttle is not an operating-system sandbox. Formatter order is
fixed to isort then Black for protocol 1; non-Python jobs skip formatting and
do not repeat checks. Phase 15 revalidates declared transaction files after
each executable stage and records unrelated final workspace changes. Default
ignored paths include VCS metadata, virtual environments, dependency trees,
PatchShuttle runtime state, and common Python caches.

The command maps approval, workspace locking, job-ID conflicts, actions,
checks, formatting, and rollback failures to the documented process exit-code
groups. A successful
check that leaves an undeclared path change returns
`UNEXPECTED_WORKSPACE_CHANGE`; PatchShuttle rolls back its declared files but
does not delete or restore that external side effect. `RunResult` and execution
errors expose the final `WorkspaceComparison`, log path, and archived job path.

## Manual rollback

Roll back a completed patch interactively or with explicit automation:

```bash
patchshuttle rollback PATCH-001
patchshuttle rollback PATCH-001 --yes
```

The Python equivalent is `rollback_job(workspace, "PATCH-001",
approved=True)`. Manual rollback reloads the retained manifest and original
copies, validates their identity and integrity, and compares every tracked
path with the exact completed-job state. It refuses to overwrite a later user
edit or remove a created directory containing undeclared entries. On success,
it restores original files, removes only paths created by that job, marks the
manifest `ROLLED_BACK`, records a fixed-section log, updates the registry, and
allows the same job ID and hash to be applied again. A failure preserves the
backup and reports unresolved paths without claiming restoration succeeded.

## Recorded runs and AI handoff

For CLI execution, PatchShuttle preserves the input `.psh.yaml` byte-for-byte
in `patches/applied/` or `patches/failed/`. A Python API call without
`source_path=` archives a deterministic YAML rendering of the immutable job
model. Archive and log filenames include the configured-timezone timestamp,
job ID, and, for archives, a short normalized hash. Numeric suffixes prevent
same-second collisions.

Every job run log contains all standard sections in a fixed order, using
`NOT_APPLICABLE` where a stage did not run, and ends with
`PATCHSHUTTLE_AI_HANDOFF`. Check and formatter output is bounded by local
policy. Common password, token, API-key, authorization-header, and private-key
shapes are masked when redaction is enabled. Redaction is best-effort and is
not a guarantee that a log contains no secrets; review a log before uploading
it to any AI service.

Find the latest upload-friendly log:

```bash
patchshuttle logs --last
```

Inspect all registered jobs or one job ID:

```bash
patchshuttle status
patchshuttle status PATCH-001
```

The registry enforces stable identity. A completed ID with the same normalized
hash returns `ALREADY_APPLIED` without rerunning actions, checks, or formatters.
The same ID with different normalized content returns `PATCH_ID_CONFLICT`
before mutable planning. Failed, rolled-back, and declined jobs may be retried
with the same ID and hash. Registry writes and the definitive identity check
share the same workspace lock as the project transaction.

Create a metadata-only project snapshot or a compact AI handoff:

```bash
patchshuttle snapshot
patchshuttle handoff
```

Both commands write timestamped `.log` files under `patches/logs/`. A snapshot
contains versions, a bounded tree, sizes and SHA-256 hashes, Git status when
available, recent jobs, capabilities, and policy summaries. It does not dump
source-file contents. A handoff adds a provider-neutral AI instruction, latest
run summary and `PATCHSHUTTLE_AI_HANDOFF` block, bounded tree, recent history,
and the explicit requirement to return one `.psh.yaml` file.

## Inspect local path policy

The planner uses the same public read-only policy API. It normalizes an
AI-supplied path, applies the local protected-path rules, inspects every
existing component without following symbolic links, and confirms that the
resolved result remains in the workspace:

```python
from patchshuttle import Policy, PolicyError, discover_workspace

policy = Policy(discover_workspace("."))

try:
    target = policy.resolve("src/example.py", allow_missing=True)
except PolicyError as error:
    print(error.code.value, error.path)
else:
    print(target.relative, target.kind.value)
```

Absolute and drive-qualified paths, URLs, parent traversal, protected paths,
symbolic-link targets or parents, sockets, devices, and named pipes are
rejected with stable error codes. Both `/` and `\` are treated as separators
so a job cannot use platform-specific spelling to bypass a rule.

Protected and ignored glob patterns come only from
`patches/patchshuttle.toml`. `**` matches zero or more complete path segments,
protected-path exceptions override configured protected globs, and matching
uses platform-appropriate case behavior. The workspace root requires explicit
`allow_root=True` access and `patches/` remains a hard block for job targets.
`Policy.is_ignored()` is used by bounded inventory, audit traversal, snapshot,
and handoff generation.

Calling `Policy` directly performs no action planning or file writes.

Run the scaffold checks:

```bash
python -m isort --check-only src tests tools
python -m black --check src tests tools
python -m coverage erase
python -m coverage run -m pytest -q
python -m coverage report --fail-under=100
python -m build
python -m twine check dist/*
python tools/release_checks.py dist
python tools/wheel_smoke.py dist/patchshuttle-0.1.0a2-py3-none-any.whl --version 0.1.0a2
```

The release candidate includes GitHub Actions for the required Ubuntu and
Windows compatibility matrix, TestPyPI qualification, and PyPI Trusted
Publishing. The required GitHub-hosted matrix first passed on 2026-08-16.
Follow [docs/RELEASE.md](docs/RELEASE.md) in order, rerun CI after every
release-candidate change, and do not treat CI as proof that either package
index passed.

## Manual workflow

The implemented local cycle is:

1. Give an AI the latest PatchShuttle handoff or log.
2. Receive one declarative `.psh.yaml` job.
3. Review the local execution plan.
4. Execute an audit, approved patch, or approved verification job.
5. For a patch, run controlled checks, isort, Black, and final checks.
6. Receive a timestamped log or generate a fresh handoff.
7. Review the log, upload it to the AI, and continue with the next job.

See [SPEC_V0_1.md](SPEC_V0_1.md) for the approved product contract and
[CHANGELOG.md](CHANGELOG.md) for release notes. PatchShuttle is designed for
ChatGPT and other AI services, but the phrase `Tested with ChatGPT` is reserved
for a documented end-to-end run using a published protocol build.

## License

PatchShuttle is licensed under the MIT License.
