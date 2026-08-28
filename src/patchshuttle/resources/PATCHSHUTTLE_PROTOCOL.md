# PatchShuttle Protocol 1

Project ID for this workspace: `{{PROJECT_ID}}`

PatchShuttle jobs are single UTF-8 YAML documents whose filenames end exactly
with `.psh.yaml`. Unknown fields, duplicate mapping keys, custom YAML tags,
anchors, and aliases are rejected.

## Top-level fields

- `protocol` - required integer `1`.
- `project_id` - required workspace identifier.
- `id` - required stable job identifier matching
  `[A-Z][A-Z0-9_-]{2,63}`.
- `kind` - `audit`, `patch`, or `verify`.
- `title` - optional text.
- `description` - optional text.
- `actions` - action list.
- `checks` - check list.

An `audit` job requires audit actions and forbids checks. A `patch` job requires
change actions. A `verify` job requires checks and forbids actions.

Each action or check list entry is a mapping containing exactly one operation
name.

## Audit actions

- `tree`: `path`, `depth`, `max_entries`, `include_hidden`.
- `read`: `path`, `start_line`, `end_line`, `max_bytes`.
- `search`: `path`, `text`, `glob`, `case_sensitive`, `max_results`.
- `search_context`: the `search` fields plus bounded `before` and `after`
  physical-line counts.
- `read_symbol`: Python `path`, dotted `symbol`, and optional `max_bytes`.
- `python_structure`: optional `path`, `max_files`, `max_symbols`, and `compact`.
- `find_files`: `path`, `glob`, `max_results`.
- `file_info`: `path`.
- `hash`: `path`, optional `algorithm: sha256`.
- `hash_range`: `path`, positive `start_line`, inclusive `end_line`, optional
  `algorithm: sha256`.
- `git_status`: empty mapping.
- `environment`: empty mapping.

`search_context` returns bounded numbered physical-line windows around literal
matches. `read_symbol` parses Python without importing project code, requires
exactly one decorator-aware class, function, method, or nested-symbol match,
and returns its physical range plus canonical LF/UTF-8 SHA-256.

`python_structure` accepts a directory or one `.py` file. `path` defaults to
`.`, `max_files` to `300`, `max_symbols` to `2000`, and boolean `compact`
to `false`. Other file targets are rejected during planning. Directory traversal
respects protected and ignored paths.

```yaml
actions:
  - python_structure:
      path: src/example
      max_files: 50
      max_symbols: 400
      compact: true
```

Full output retains the existing
`patchshuttle.python_structure_collection.v1` collection schema and
`patchshuttle.python_structure.v1` per-file schema. It reports counts, parse
errors, limit signals, top-level import records, declarations, qualified names,
parameters, decorators, bases, and physical source ranges.

Compact output uses `patchshuttle.python_structure_collection.compact.v1` and
`patchshuttle.python_structure.compact.v1`. It retains the same counts, parse
errors, and limit signals. Per-file records contain file paths, import and symbol
counts, and symbol records limited to kind, qualified name, and physical range.
Syntax errors are reported without source excerpts in both modes.

The action uses the standard-library AST without importing or executing project
code. Neither mode exposes source values or resolves calls, references, imports,
inferred types, or runtime behavior. Neither mode caches, persists, or selects
patch scope. The normal audit output limit remains authoritative.

## Change actions

- `create_directory`: `path`.
- `create_file`: `path`, `content`, optional `encoding` and `newline`.
- `replace_exact`: `path`, `old`, `new`, `expected_count`.
- `replace_symbol`: `path`, dotted `symbol`, `expected_sha256`, and
  `new_content`. The hash is the canonical value returned by `read_symbol`.
- `insert_before`: `path`, `anchor`, `content`, `expected_count`.
- `insert_after`: `path`, `anchor`, `content`, `expected_count`.
- `delete_exact`: `path`, `text`, `expected_count`.
- `replace_range`: `path`, positive `start_line`, inclusive `end_line`,
  `new_content`, plus `expected_content` and/or `expected_sha256`.
- `delete_range`: `path`, positive `start_line`, inclusive `end_line`, plus
  `expected_content` and/or `expected_sha256`.
- `insert_at_line`: `path`, positive `line`, `position: before|after`,
  non-empty `content`, plus `expected_content` and/or `expected_sha256`.
- `apply_diff`: `diff`, optional `strip` from `0` through `2`.

Line numbers are 1-based and are only positions, never proof of identity.
Every line-changing action requires at least one guard. If both guards are
provided, both must pass against the planner's sequential simulated content.
Ranges and guard content use canonical LF newlines; SHA-256 guards hash those
canonical characters encoded as UTF-8. A bounds or guard mismatch stops the
plan. PatchShuttle does not fuzz, relocate, partially apply, or automatically
shift a range.

## Local Python architecture policy

Architecture limits are workspace-local TOML policy, not protocol-1 job fields.
The fixed profile is `modular-monolith`, the organization hint is
`package-by-feature`, and the only mode is `ratchet`. Planning evaluates the
resolved virtual Python file state with the bounded workspace inventory before
execution. Warnings are reported; hard regressions fail with
`ARCHITECTURE_POLICY_FAILED`. Inventory failures fail closed with
`ARCHITECTURE_INSPECTION_FAILED`.

Stable finding codes are `ARCH001`, `ARCH002`, `ARCH010`, `ARCH011`, `ARCH020`,
and `ARCH021`. Plan and log summaries expose the active profile, organization,
mode, status, evaluated and new counts, total findings, and report-limit flag.
No source fragments are included. The policy does not add a job action, execute
project code, infer dependencies, reorganize files, or alter patch targeting.
## Structured history records

Recorded execution attempts may produce a secondary JSON artifact under
`patches/history/<job-id>/<record-id>.json`. The supported record declares
`schema: patchshuttle.history.v1` and `schema_version: 1`. Files are created with
exclusive append-only naming and are never selected by a protocol-1 job field.

Top-level fields are `record_id`, `occurred_at`, `patchshuttle_version`,
`project_id`, `job`, `redaction`, `declared`, `observed`, `references`, and
`relationships`. In v1, `relationships` is `null` because protocol 1 has no trusted
relationship metadata. `declared` contains bounded title, description-derived
intent, plan counts, planned file paths, and explicit `replace_symbol` targets.
`observed` contains terminal status and exit code, actual workspace changes, checks,
failures, rollback, check warnings, and only symbols whose targeted file was
observed as affected. `references` points to the detailed log, its non-persistent
derived AI-log view, the archived job, and backup when applicable.

The record does not copy command output, full check output, tracebacks, file or patch
contents. It does not infer workarounds, requirements, semantics, relationships, or
project memory. `job.description` remains declared intent even when it was authored
by an AI agent.

History persistence runs only after required operational recording. Any secondary
write failure is non-fatal and cannot change the execution result or rollback state.
Old jobs and old workspaces remain valid. New default workspaces ignore
`patches/history/**` during inventories; existing owners may add the same local
ignored path.

Read-only interfaces are `history list [--job-id ID] [--limit N]`,
`history latest [JOB_ID]`, and `history show JOB_ID/RECORD_ID`, plus the public
functions exported by `patchshuttle.history`. Semantic retrieval and external
database ingestion are outside the PatchShuttle protocol.

## Checks

- `compileall`: non-empty `paths`, optional `quiet` from `0` through `2`.
- `ruff`: empty mapping. For a patch, PatchShuttle derives the ordered scope
  only from changed Python files and runs fixed `F` rules with no fixes. Jobs
  cannot provide paths, rules, fixes, or Ruff arguments.
- `pytest`: optional `paths`, `args`, and `timeout_seconds`. Allowed arguments:
  `-q`, `--quiet`, `-v`, `--verbose`, `-x`, `--exitfirst`, `-s`,
  `--disable-warnings`, `--strict-config`, `--strict-markers`, positive
  `--maxfail=N`, `--tb=auto|long|short|line|native|no`, and
  `--capture=fd|sys|no|tee-sys`.
- `unittest`: `discover` and `pattern`.
- `django_check`: `manage_py`. Its Django `WARNINGS:` output is compared
  with the explicit protected baseline at
  `patches/state/warning-baseline.json`. Full and compact logs report
  known and new counts plus complete new-warning records. Truncated output
  is marked `INCOMPLETE_TRUNCATED`; classification never changes the
  process status. `patchshuttle warnings [--add ID] [--remove ID]` is the
  only supported baseline mutation path, and no ID is accepted
  automatically.
- `django_migrations_check`: `manage_py`.
- `django_test`: `manage_py` and optional dotted-identifier `labels`.
- `django_import_check`: `manage_py` and non-empty dotted Python `modules`.
- `import_check`: non-empty dotted Python `modules`.
- `profile`: `name` of a local profile already defined in
  `patches/patchshuttle.toml`.

## Project Python interpreter

`[execution].python_executable` is optional local owner policy in
`patches/patchshuttle.toml`. A relative value resolves from the workspace root,
an absolute value remains absolute, and the effective target must be an existing
regular file before a project check runs. Omission preserves `sys.executable`.
Jobs cannot set or override the value, and PatchShuttle performs no virtual
environment autodetection.

The effective project interpreter drives `compileall`, `pytest`, `unittest`,
all Django checks, `import_check`, and `{python}` in local profiles. Ruff,
isort, Black, HTML lint, quality preflight, audits, and PatchShuttle internals
continue to use PatchShuttle's own interpreter.
## Local authority and safety

Jobs cannot change project identity, protected paths, confirmation, backups,
rollback, formatting order or exclusions, HTML lint policy, command allowlists,
size limits, or shell policy. There is no arbitrary-shell action. Project
checks can execute project code with the current user's permissions, so
PatchShuttle is not an operating-system sandbox.

All job paths are relative to the workspace root. The implemented planner
rejects absolute paths, parent traversal, protected or ignored targets,
symbolic-link escapes, binary targets, special files, and files outside
configured limits.

Use `patchshuttle capabilities`, `patchshuttle schema`, and
`patchshuttle explain replace_symbol` to inspect the installed protocol without
reading generated protected files. Workspace-aware commands accept an exact
root as a global option before the command:
`patchshuttle --workspace PATH COMMAND [ARGS]`. Without it, PatchShuttle
searches the current directory and its parents. A bounded direct-child hint may
be printed after failed implicit discovery, but no candidate is selected
automatically. Job-file arguments remain relative to the process current
directory.

Use `patchshuttle validate patches/inbox/JOB.psh.yaml`, then review
`patchshuttle plan patches/inbox/JOB.psh.yaml`. Planning is read-only and does
not mean the job was applied. Add `--diff` to display a bounded unified preview
of the final resolved bytes. Exact-count failures include exact line numbers or
nearby similarity-ranked snippets; unified-diff failures include hunk counts
and first context mismatches. Line-range failures report the requested bounds,
current total lines, guard types, current digest, and bounded content previews
when applicable. `patchshuttle run JOB.psh.yaml` accepts every job
kind. `patchshuttle audit JOB.psh.yaml` requires an audit job and does not
prompt. `patchshuttle verify JOB.psh.yaml` requires a verify job and, like a
patch, uses a deny-by-default confirmation unless `--yes` is supplied. The
public Python equivalent is `execute_plan(plan, approved=True)`; audit plans do
not require approval. `patchshuttle run PATCH.psh.yaml --keep-changes` is a
user-controlled patch-only mode that requires separate confirmation unless
combined with `--yes`. It is not a YAML field and local policy may forbid it.

Audit output and traversal are bounded and a workspace comparison verifies
read-only behavior. Verify jobs run their checks once and record workspace side
effects. Planning records baseline and final-planned isort/Black compatibility
for each non-excluded changed Python path. It rejects a newly introduced
formatter incompatibility separately from a pre-existing incompatibility that
remains after the change. Exact `.py` exclusions may be configured only by the
workspace owner in `[formatting]`; a job cannot add them. Patch execution can
include locally enabled changed-HTML djLint checks, followed by controlled
initial checks, per-tool scoped isort then Black, repeated final checks,
defensive removal of new runtime `.pyc` files within the changed-Python scope,
bounded before/after workspace inventory, and automatic rollback. Final
changes outside declared transaction paths and configured ignored paths are
reported as `UNEXPECTED_WORKSPACE_CHANGE`.
HTML content is passed through stdin from an isolated configuration root;
project `pyproject.toml`, `djlint.toml`, and `.djlintrc` settings cannot override
the local PatchShuttle lint profile or ignore list.
Declared patch paths are rolled back; reported external check side effects are
not removed automatically. If the user explicitly accepts `--keep-changes`, a
failed patch retains published declared changes and records
`SKIPPED_CHANGES_KEPT`; if no declared change was published, it records
`SKIPPED_NO_CHANGES`.

Every recorded job attempt writes a timestamped fixed-section log, archives
the exact CLI job in `applied/` or `failed/`, and atomically updates the
registry. The same completed ID and hash returns `ALREADY_APPLIED`; different
normalized content under an existing ID returns `PATCH_ID_CONFLICT`. Use
`patchshuttle rollback PATCH_ID` for guarded manual restoration of a completed
patch, `snapshot` for bounded project metadata, `handoff` for upload-friendly
AI context, `logs --last` for the newest log, and `status [JOB_ID]` for registry
state. Log redaction is best-effort. It masks common credential shapes and
sensitive Python assignments, including prefixed password, token, API-key,
access-key, secret-key, private-key, and one-line secret-collection values,
while preserving safe references and loader calls. Review every log before
sharing it. The JSON Schema in `patchshuttle.schema.json` is the exact
structural schema produced by the installed model version.

Run logs and early validation/planning failure logs contain a
`PYTHON_DISCOVERY_EVALUATION` section. Its evidence scope is the current job.
It reports explicit `.py` paths, executed Python-targeted audit actions,
bounded audit output byte/line counts before whole-log redaction, declared
text/symbol/line targeting,
and selected targeting failure signals. Unscoped searches are counted
separately. The section performs no project scan, token estimate, semantic
resolution, historical aggregation, or symbol-index recommendation;
`index_assessment` remains `NOT_EVALUATED`.

The section also aggregates `matches`, `result_limit_reached`, and
`duration_ms` already recorded by completed Python-targeted audit actions.
Default policy for newly initialized workspaces ignores and protects nested
`.venv`, `venv`, and `node_modules` trees. Existing configuration is
owner-controlled and is not migrated automatically; repository-specific
generated or backup directories belong in `project.ignored_paths`.

A validation or planning failure after workspace resolution writes a smaller
timestamped `VALIDATION_FAILED` or `PLAN_FAILED` log with `SUMMARY` and
`PATCHSHUTTLE_AI_HANDOFF` sections. It does not archive invalid source, update
the registry, claim project changes, or create a backup. Successful standalone
validation and planning remain artifact-free. If workspace discovery itself
fails, there is no selected workspace in which to write the failure log.
