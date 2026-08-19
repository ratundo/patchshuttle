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
- `find_files`: `path`, `glob`, `max_results`.
- `file_info`: `path`.
- `hash`: `path`, optional `algorithm: sha256`.
- `git_status`: empty mapping.
- `environment`: empty mapping.

## Change actions

- `create_directory`: `path`.
- `create_file`: `path`, `content`, optional `encoding` and `newline`.
- `replace_exact`: `path`, `old`, `new`, `expected_count`.
- `insert_before`: `path`, `anchor`, `content`, `expected_count`.
- `insert_after`: `path`, `anchor`, `content`, `expected_count`.
- `delete_exact`: `path`, `text`, `expected_count`.
- `apply_diff`: `diff`, optional `strip` from `0` through `2`.

## Checks

- `compileall`: non-empty `paths`, optional `quiet` from `0` through `2`.
- `pytest`: optional `paths`, `args`, and `timeout_seconds`. Allowed arguments:
  `-q`, `--quiet`, `-v`, `--verbose`, `-x`, `--exitfirst`, `-s`,
  `--disable-warnings`, `--strict-config`, `--strict-markers`, positive
  `--maxfail=N`, `--tb=auto|long|short|line|native|no`, and
  `--capture=fd|sys|no|tee-sys`.
- `unittest`: `discover` and `pattern`.
- `django_check`: `manage_py`.
- `django_migrations_check`: `manage_py`.
- `django_test`: `manage_py` and optional dotted-identifier `labels`.
- `import_check`: non-empty dotted Python `modules`.
- `profile`: `name` of a local profile already defined in
  `patches/patchshuttle.toml`.

## Local authority and safety

Jobs cannot change project identity, protected paths, confirmation, backups,
rollback, formatting order, HTML lint policy, command allowlists, size limits,
or shell policy. There is no arbitrary-shell action. Project checks can execute
project code with the current user's permissions, so PatchShuttle is not an
operating-system sandbox.

All job paths are relative to the workspace root. The implemented planner
rejects absolute paths, parent traversal, protected or ignored targets,
symbolic-link escapes, binary targets, special files, and files outside
configured limits.

Use `patchshuttle validate patches/inbox/JOB.psh.yaml`, then review
`patchshuttle plan patches/inbox/JOB.psh.yaml`. Planning is read-only and does
not mean the job was applied. Add `--diff` to display a bounded unified preview
of the final resolved bytes. Exact-count failures include exact line numbers or
nearby similarity-ranked snippets; unified-diff failures include hunk counts
and first context mismatches. `patchshuttle run JOB.psh.yaml` accepts every job
kind. `patchshuttle audit JOB.psh.yaml` requires an audit job and does not
prompt. `patchshuttle verify JOB.psh.yaml` requires a verify job and, like a
patch, uses a deny-by-default confirmation unless `--yes` is supplied. The
public Python equivalent is `execute_plan(plan, approved=True)`; audit plans do
not require approval. `patchshuttle run PATCH.psh.yaml --keep-changes` is a
user-controlled patch-only mode that requires separate confirmation unless
combined with `--yes`. It is not a YAML field and local policy may forbid it.

Audit output and traversal are bounded and a workspace comparison verifies
read-only behavior. Verify jobs run their checks once and record workspace side
effects. Planning requires changed Python files to pass PEP 263 encoding,
isort, and Black compatibility checks. Patch execution can include locally
enabled changed-HTML djLint checks, followed by controlled initial checks,
scoped isort then Black, repeated final checks, bounded before/after workspace
inventory, and automatic rollback. Final changes outside declared transaction
paths and configured ignored paths are reported as
`UNEXPECTED_WORKSPACE_CHANGE`.
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
state. Log redaction is best-effort, so review a log before sharing it. The JSON
Schema in `patchshuttle.schema.json` is the exact structural schema produced by
the installed model version.
