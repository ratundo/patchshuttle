# PatchShuttle

PatchShuttle is intended to provide local, auditable patch workflows for
step-by-step software development with ChatGPT and other AI services. An AI
describes a small job, the user reviews and runs it locally, and PatchShuttle
records the result for the next iteration.

> [!IMPORTANT]
> PatchShuttle is alpha software. Version `0.1.0a3` implements the complete
> local v0.1 workflow. It executes bounded read-only
> audits, approved patch transactions, and approved one-pass verification jobs
> under a project lock. Patch jobs retain backups, run controlled checks, apply
> scoped isort then Black, repeat checks, and compare SHA-256 workspace
> inventories. Completed patches support guarded manual rollback. Timestamped
> fixed-section logs, exact job archives, registry idempotency, project
> snapshots, AI handoffs, declarative Python constructors, release checks, and
> Trusted Publishing workflows are implemented. Every versioned release must
> pass its own local qualification, GitHub-hosted Windows/Ubuntu matrix,
> TestPyPI installation, production publication, and post-release smoke gates.
> The immutable `0.1.0a2` and `0.1.0a3` qualification evidence, plus the
> separately scoped `0.1.0a2` ChatGPT end-to-end workflow, are retained in
> [docs/RELEASE.md](docs/RELEASE.md).

> [!NOTE]
> Compared with `0.1.0a2`, version `0.1.0a3` adds AI-facing planner diagnostics,
> formatter preflight, optional HTML linting, failure-attempt logs, explicit
> workspace routing, workspace-independent self-documentation, per-file
> formatter policy, a Django-aware import check, defensive runtime-cache
> cleanup, and guarded physical-line range actions with a canonical read-only
> range hash.

## Design goals

- Keep every change local and explicitly initiated by the user.
- Make each run reviewable, reproducible, and easy to return to an AI as a log.
- Centralize recurring safety checks, backups, tests, isort, and Black.
- Support both existing repositories and projects built from an empty folder.
- Use the same typed job model from YAML, the CLI, and the Python API.

PatchShuttle is not a security sandbox. Project tests and AI-generated project
code can execute arbitrary behavior with the current user's permissions.

## Install

After publication, install the exact alpha release from PyPI:

```bash
python -m venv .venv
python -m pip install --upgrade pip
python -m pip install "patchshuttle==0.1.0a3"
```

For development from a local checkout:

```bash
python -m pip install -e ".[dev]"
```

To enable opt-in HTML template linting in an installed release, install the
`html` extra:

```bash
python -m pip install "patchshuttle[html]==0.1.0a3"
```

For a local development checkout, install both development and HTML extras:

```bash
python -m pip install -e ".[dev,html]"
```

The extra installs djLint. HTML linting still remains disabled until the local
workspace configuration explicitly enables it.

Formatter exclusions are explicit user-owned local policy. New workspaces
contain empty lists in `[formatting]`; an older workspace may add the keys to
its existing section:

```toml
[formatting]
enabled = true
order = ["isort", "black"]
scope = "changed_python_files"
rerun_checks = true
isort_exclude = []
black_exclude = ["email_client/views.py"]
```

Each entry must be one exact normalized workspace-relative `.py` path. An
excluded file still receives the requested project checks. The exclusion only
skips the named formatter, is shown in the plan and log, and cannot be supplied
or changed by an AI job. Strict formatting remains the default.

For a newly initialized workspace, edit the generated block in
`patches/patchshuttle.toml`. For an older workspace that does not yet contain
the block, append it:

```toml
[linting.html]
enabled = true
tool = "djlint"
profile = "django"
scope = "changed_html_files"
ignore = []
```

Choose the profile that matches the project: `html`, `django`, `jinja`,
`nunjucks`, `handlebars`, `liquid`, `golang`, `angular`, `tera`, or `askama`.
The ignore list accepts explicit djLint rule codes such as `H006`. These values
are user-owned local policy and cannot be supplied by a job.

Verify the installed CLI:

```bash
patchshuttle version
patchshuttle --help
```

In version `0.1.0a3`, an AI or user can inspect the installed contract without
initializing a workspace or reading protected generated files:

```bash
patchshuttle capabilities
patchshuttle schema
patchshuttle explain replace_exact
patchshuttle explain replace_range
patchshuttle explain hash_range
patchshuttle explain apply_diff
```

`capabilities` prints the finite protocol surface and safety boundaries,
`schema` prints the exact deterministic JSON Schema produced by the installed
job model, and `explain` describes supported high-friction actions and workflow
topics. Run `patchshuttle explain --help` for the finite topic list.

## Initialize a workspace

Run this inside an existing project:

```bash
patchshuttle init
```

For a new project, start in an empty directory:

```bash
patchshuttle init --new-project
```

Every workspace-aware command can also receive an exact root before the
command name:

```bash
patchshuttle --workspace path/to/project init
patchshuttle --workspace path/to/project handoff
```

Without this option, PatchShuttle searches the current directory and its
parents. If no workspace is found, the CLI performs a bounded scan of direct
child directories and may print candidate names and a rerun hint. It
does not select or execute against a candidate automatically.
`JOB_FILE` arguments remain relative to the process current directory; the
workspace option changes workspace selection, not normal path-argument rules.

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

The current `validate` command reads the selected workspace configuration,
applies its `max_job_bytes` limit, safely loads the YAML, validates the typed
model, and compares the job project ID. It does not plan or execute actions or
modify project source files. A successful validation does not create an
operational artifact. In version `0.1.0a3`, a validation failure after the
workspace is resolved writes a timestamped `VALIDATION_FAILED` log containing
the stable error, summary, and AI handoff. It does not archive invalid source
as a job or update the registry. A workspace-discovery failure has no resolved
workspace in which to write a log.

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

For modules that require Django settings or app-registry initialization, use
the controlled Django-aware check instead of the plain `import_check`:

```yaml
checks:
  - django_import_check:
      manage_py: manage.py
      modules: [email_client.views, email_client.urls]
```

PatchShuttle runs the current interpreter with `manage.py shell -c` and
internally generated import code. The YAML accepts only bounded dotted module
identifiers, not an arbitrary expression or shell command.

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
patchshuttle plan patches/examples/PATCH-EXAMPLE.psh.yaml --diff
```

The command displays the normalized job hash, sequential action dispositions,
files and directories that would be created or modified, requested checks,
Python formatting scope, a per-file isort/Black decision matrix, optional HTML
lint scope, successful quality preflight records, protected-path result, backup
destination template, rollback policy, and whether later execution will
require confirmation.
`--diff` also prints a bounded unified preview of the final resolved bytes
without writing them.

Planning performs the complete implemented read-only preflight:

- applies local action-count, check, size, ignored-path, and protected-path
  policy;
- simulates sequential changes in memory, including changes to a file created
  earlier in the same job;
- checks exact occurrence counts and idempotent `NO_CHANGE` states;
- reports exact-match line numbers or up to three bounded, similarity-ranked
  nearby snippets when an occurrence count does not match;
- parses and dry-runs text-only unified diffs without a shell or external
  `patch` command, with hunk count and first-context-mismatch diagnostics;
- rejects binary content, unsupported target encodings, mixed newline styles,
  symbolic links, special files, and file-size violations;
- validates check paths, the conservative pytest argument allowlist, dotted
  Django labels, local profiles, and required Python modules;
- detects Python source encodings with the PEP 263 mechanism and records
  separate baseline and final-planned compatibility for isort and Black on
  every non-excluded changed-Python file;
- reports `FORMATTER_BASELINE_INCOMPATIBLE` when both the existing and planned
  file remain incompatible, `FORMATTER_PATCH_INCOMPATIBLE` when the planned
  change introduces incompatibility, and allows a plan that repairs an
  incompatible baseline;
- when locally enabled, passes every final planned changed `.html` file to
  djLint through stdin from an isolated temporary configuration root before
  confirmation;
- records target fingerprints and computes final bytes and SHA-256 hashes for
  the internal transactional runner.

Policy blocks exit with code `4`, planning failures with code `5`, and missing
check profiles or dependencies with code `9`. A successful plan exits with
code `0`. In version `0.1.0a3`, a failed planning attempt after workspace
resolution writes a timestamped `PLAN_FAILED` log. The terminal result prints
its path, and `patchshuttle logs --last` returns that failure log until a newer
recorded artifact is written.

The same planner is available from Python:

```python
from patchshuttle import discover_workspace, load_job, plan_job, render_plan_diff

workspace = discover_workspace(".")
job = load_job(
    "patches/inbox/PATCH-001.psh.yaml",
    max_bytes=workspace.config.execution.max_job_bytes,
)
plan = plan_job(job, workspace)

print(plan.job_hash)
print(plan.files_to_create)
print(plan.files_to_modify)
print(render_plan_diff(plan).text)
```

`Plan`, its actions, checks, fingerprints, and final file changes are immutable.
Planning does not create target directories or files, execute audits or checks,
write formatter or linter output, create backups, or alter registry state. A
successful plan does not create a log. A failed plan may create only its
managed failure log. Planning may invoke formatter libraries and the optional
HTML linter against in-memory final content for compatibility preflight. A
successful plan therefore does not mean that the job was applied.

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
SHA-256 `hash`, canonical `hash_range`, `git_status`, and `environment`.
Traversal, file reads, match counts, and recorded output are bounded by local
policy. Protected and ignored paths are skipped, source content appears only
when explicitly requested by a `read` or `search` action, and a before/after
inventory verifies that the audit did not modify the workspace.

A verify job runs its controlled checks once, does not create a backup or run
formatters, and compares the workspace before and after. A successful check
that changes a non-ignored path returns `UNEXPECTED_WORKSPACE_CHANGE`; because
project checks can have external effects, PatchShuttle reports but does not
automatically undo those changes.

A patch plan may contain `create_directory`, `create_file`, `replace_exact`,
`insert_before`, `insert_after`, `delete_exact`, `replace_range`,
`delete_range`, `insert_at_line`, or `apply_diff` actions and applies the final
bytes already computed by the planner.

The optional line-range actions are strict guarded operations for cases where
a current audit already established an exact physical range. Lines are 1-based
and ranges are inclusive. `expected_content`, `expected_sha256`, or both prove
the target; line numbers only locate it. If both guards are supplied, both must
pass against the sequential in-memory plan. Canonical guards use LF newlines
and SHA-256 over UTF-8 bytes. PatchShuttle never fuzzes, relocates, partially
applies, or automatically shifts a stale range. Use an audit `hash_range` when
a digest is more compact than repeating a large old block.

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
7. when HTML linting is locally enabled, run djLint in lint-only mode on the
   exact changed `.html` scope and require it not to modify transaction files;
8. build fixed argument arrays for every requested check and run them in order
   from the workspace root with `shell=False`, per-check timeout, separate
   stdout and stderr capture, local output truncation limits, and Python
   bytecode caches redirected to an isolated temporary directory;
9. stop on the first failed, timed-out, or unstartable initial check and verify
   that the checks did not change any declared transaction file;
10. resolve the per-file local formatter policy, then run isort followed by
    Black only on each tool's exact non-excluded relative paths using the
    current interpreter in isolated mode, fixed argument arrays, `shell=False`,
    and the same timeout and bounded output controls;
11. stop on formatter failure, reject changes to declared non-Python files,
   reject oversized or non-regular formatter targets, and retain the formatted
   bytes, SHA-256 hashes, sizes, and modes;
12. when configured, repeat the same checks and require every formatted and
    non-formatted transaction file to retain its exact approved post-state;
13. remove only new regular `.pyc` files and newly empty `__pycache__`
    directories within the bounded changed-Python scope, preserving every
    pre-existing or foreign entry and treating unresolved cache paths as a
    transaction failure;
14. capture the final bounded inventory and classify added, removed, modified,
    and type-changed paths as declared or unexpected;
15. mark the manifest `COMPLETED`, or restore modified originals and remove
    only paths created by this attempt before recording `ROLLED_BACK` or
    `ROLLBACK_FAILED`; an explicitly accepted `--keep-changes` run instead
    records `CHANGES_KEPT` when a failed job published declared changes;
16. archive the exact CLI source job, write a fixed-section UTF-8 log with a
   compact AI handoff block, and atomically commit registry state before
   releasing the workspace lock.

A no-change plan is revalidated under the lock and returns without creating a
backup or launching checks. A race detected before replacement is not
overwritten. Rollback validates retained originals before restoring them,
refuses to follow symbolic links or remove foreign non-empty directories, and
does not claim success when a tracked path cannot be restored.

The internal check runner supports `compileall`, `pytest`, `unittest`, Django
checks and tests, plain validated module imports, Django-aware validated module
imports through `manage.py shell -c`, and locally configured profiles. It
inherits the current process environment because project checks execute project
code and PatchShuttle is not an operating-system sandbox. PatchShuttle overrides
`PYTHONPYCACHEPREFIX` with a fresh temporary directory for each check and
removes that directory afterward, keeping ordinary generated Python bytecode
outside the workspace. A defensive cache ledger also handles `.pyc` files
created inside newly added packages despite that override. Formatter order is
fixed to isort then Black for protocol 1; local exact-path exclusions are
resolved separately for each tool. Non-Python jobs skip formatting and do not
repeat checks. Optional djLint uses a locally selected template profile,
targets only changed `.html` files, never reformats them, and triggers the same
rollback path on failure. It reads content through stdin from an isolated
temporary configuration root, so project djLint configuration cannot weaken
the local PatchShuttle lint policy. Transaction files are revalidated after
each executable stage and unrelated final workspace changes are recorded.
Default ignored paths include VCS metadata, virtual environments, dependency
trees, PatchShuttle runtime state, and common Python caches.

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

Every job run log contains all standard sections in a fixed order, including
`LINT_HTML`, using `NOT_APPLICABLE` where a stage did not run, and ends with
`PATCHSHUTTLE_AI_HANDOFF`. Check and formatter output is bounded by local
policy, as is HTML linter output. Common password, token, API-key,
authorization-header, and private-key shapes are masked when redaction is
enabled. Redaction is best-effort and is not a guarantee that a log contains no
secrets; review a log before uploading it to any AI service.

Early `VALIDATION_FAILED` and `PLAN_FAILED` logs use a smaller fixed attempt
format with `SUMMARY` and `PATCHSHUTTLE_AI_HANDOFF` sections. They record no
project changes, backup, job archive, or registry update. An explicitly
declined reviewed plan continues to use the full job log and exact failed-job
archive with result `USER_DECLINED`.

Find the latest upload-friendly log:

```bash
patchshuttle logs --last
```

This selects the newest recorded run, snapshot, handoff, or failure-attempt
log. Commands that intentionally produce no artifact, including successful
`validate`, successful `plan`, `version`, `capabilities`, `schema`, and
`explain`, do not replace it.

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
python tools/wheel_smoke.py dist/patchshuttle-0.1.0a3-py3-none-any.whl --version 0.1.0a3
```

The `0.1.0a2` and `0.1.0a3` alpha releases each passed the required Ubuntu and
Windows compatibility matrix, TestPyPI qualification, production PyPI Trusted
Publishing, and a clean post-release installation. Their exact release and
workflow links and artifact hashes, together with the separately scoped
`0.1.0a2` ChatGPT end-to-end evidence, are recorded in
[docs/RELEASE.md](docs/RELEASE.md). Follow that guide in order for every future
release and append separate immutable evidence after each gate completes.
Rerun CI after every release-candidate change.

## Manual workflow

The implemented local cycle is:

1. Give an AI the latest PatchShuttle handoff or log.
2. Receive one declarative `.psh.yaml` job.
3. Review the local execution plan.
4. Execute an audit, approved patch, or approved verification job.
5. For a patch, run optional changed-HTML linting, controlled checks, the
   locally resolved isort/Black scopes, and final checks.
6. Receive a timestamped log or generate a fresh handoff.
7. Review the log, upload it to the AI, and continue with the next job.

See [SPEC_V0_1.md](SPEC_V0_1.md) for the approved product contract and
[CHANGELOG.md](CHANGELOG.md) for release notes. PatchShuttle is designed for
ChatGPT and other AI services. Version `0.1.0a2` is **Tested with ChatGPT** for
a recorded audit, patch, formatting, repeated-check, and independent
verification workflow on Windows 11 with Python 3.14.2. This statement applies
to that documented workflow, not to every supported action, failure path, AI
model, or project.

## License

PatchShuttle is licensed under the MIT License.
