# PatchShuttle AI Guide

This file is the provider-neutral contract for ChatGPT and other AI services
that prepare PatchShuttle jobs for this project.

## Project identity

- Protocol: `1`
- Project ID: `{{PROJECT_ID}}`
- Canonical job extension: `.psh.yaml`

Every job must use exactly this project ID. Do not copy an ID from another
project or invent a replacement.

## Required AI response

When asked for a PatchShuttle job:

1. Return exactly one UTF-8 YAML document.
2. Do not wrap it in Markdown fences.
3. Do not add prose before or after the YAML.
4. Keep the job focused on one reviewable goal.
5. Use only the actions and checks listed below.
6. Never provide shell commands, Python code to execute the job, or policy
   overrides.
7. Preserve the same job ID when correcting a rejected job only when the user
   explicitly requests a correction of that same attempt.
8. Never add confirmation or rollback choices to YAML. `--yes` and
   `--keep-changes` are user-controlled CLI decisions, not job fields.
9. Never add formatter settings, formatter exclusions, or HTML linter settings
   to YAML. Those settings belong only to the user's local
   `patches/patchshuttle.toml` policy.

## Job kinds

- `audit` - one or more read-only audit actions, no checks.
- `patch` - one or more change actions and appropriate checks.
- `verify` - one or more checks, no actions.

## Audit actions

- `tree` - bounded directory tree.
- `read` - bounded text-file excerpt.
- `search` - literal text search.
- `find_files` - bounded glob search.
- `file_info` - file metadata.
- `hash` - SHA-256 file hash.
- `git_status` - Git status when Git is available.
- `environment` - bounded development-environment summary.

## Change actions

- `create_directory` - create one directory.
- `create_file` - create one new text file.
- `replace_exact` - replace an exact text occurrence count.
- `insert_before` - insert text before an exact anchor.
- `insert_after` - insert text after an exact anchor.
- `delete_exact` - delete exact text with an expected count.
- `apply_diff` - apply a unified diff to existing text files.

## Checks

- `compileall`
- `pytest` - optional arguments are limited to `-q`, `--quiet`, `-v`,
  `--verbose`, `-x`, `--exitfirst`, `-s`, `--disable-warnings`,
  `--strict-config`, `--strict-markers`, positive `--maxfail=N`,
  `--tb=auto|long|short|line|native|no`, and
  `--capture=fd|sys|no|tee-sys`.
- `unittest`
- `django_check`
- `django_migrations_check`
- `django_test` - labels must be dotted Python identifiers.
- `django_import_check` - use for dotted modules that require Django settings
  or app-registry initialization; include `manage_py` and `modules`.
- `import_check`
- `profile` - only a profile already defined by the user in
  `patches/patchshuttle.toml`.

## Audit example

```yaml
protocol: 1
project_id: {{PROJECT_ID}}
id: AUDIT-001
kind: audit
title: Inspect the project structure
actions:
  - tree:
      path: .
      depth: 4
  - git_status: {}
```

## Patch example

```yaml
protocol: 1
project_id: {{PROJECT_ID}}
id: PATCH-001
kind: patch
title: Create a small Python module
actions:
  - create_directory:
      path: src/example
  - create_file:
      path: src/example/__init__.py
      content: |
        VALUE = 1
checks:
  - compileall:
      paths: [src]
```

## Verify example

```yaml
protocol: 1
project_id: {{PROJECT_ID}}
id: VERIFY-001
kind: verify
title: Run the project tests without changing files
checks:
  - pytest:
      paths: [tests]
      args: [-q]
```

When an import requires Django initialization, request the dedicated check:

```yaml
checks:
  - django_import_check:
      manage_py: manage.py
      modules: [email_client.views, email_client.urls]
```

## Local workflow

The user saves the YAML in `patches/inbox/`, validates it, runs
`patchshuttle plan`, and reviews the read-only plan locally. The user may add
`--diff` to review the bounded final resolved diff before execution. An audit
can run without confirmation. Patch and verify jobs require explicit
confirmation or deliberate `--yes` automation. If validation or planning
fails after workspace resolution, ask for the generated failure `.log`; its
path is printed in the terminal and available through
`patchshuttle logs --last`. Use reported exact line numbers, nearby similarity
matches, or
unified-diff hunk diagnostics to correct the next job instead of guessing file
content. After every recorded attempt, ask the user for that `.log` file or a
fresh `patchshuttle handoff` file and use its final
`PATCHSHUTTLE_AI_HANDOFF` block before preparing the next job. Never state that
a job was applied merely because YAML or a successful plan was created.

When operation syntax is unclear, ask the user to run
`patchshuttle capabilities`, `patchshuttle schema`, or
`patchshuttle explain TOPIC`. These commands inspect the installed contract
without requiring audit access to protected generated documentation. If the
current directory is outside the project, the user can place a global
`--workspace PATH` option before the command. A reported child-workspace hint
is advisory; never assume that candidate was selected.

Read the formatter matrix in every plan. `RUN` means that formatter will touch
that exact path. `SKIP_LOCAL_POLICY` is an owner-controlled exception and must
not be copied into YAML. `FORMATTER_PATCH_INCOMPATIBLE` means the planned
content is incompatible after a compatible or absent baseline, so correct the
patch. `FORMATTER_BASELINE_INCOMPATIBLE` means the file was already
incompatible and remains so; do not ask to bypass safety policy. The owner may
deliberately add an exact local exclusion, or the next patch may repair the
file. A plan with `baseline=INCOMPATIBLE, planned=PASS` is a valid repair.

Interpret failure states conservatively:

- `ROLLED_BACK` means declared transaction paths were restored, but reported
  external check side effects still require review.
- `SKIPPED_CHANGES_KEPT` means the user explicitly retained partial declared
  changes. Request a fresh audit or handoff before producing a corrective job.
- `SKIPPED_NO_CHANGES` means rollback was skipped before a declared change was
  published; still use the log as the authoritative result.
- `ROLLBACK_FAILED` means restoration is incomplete. Stop proposing new
  changes until the user resolves the listed paths.

This build provides workspace initialization, validation, read-only
planning, bounded audit execution, approved patch execution, and approved
one-pass verification. Patch execution uses all text change actions,
optional changed-HTML linting under local policy, controlled initial checks,
per-tool changed-Python isort then Black scopes, final checks, bounded
before/after workspace inventory, defensive runtime-cache cleanup, undeclared
side-effect reporting, rollback of declared transaction paths, exact CLI job
archives, an atomic registry, and fixed-section redacted logs. Planning records
PEP 263 Python encoding plus baseline and final-planned formatter compatibility
before confirmation. HTML content is linted through
stdin from an isolated configuration root, so a job cannot weaken local lint
policy through project djLint settings. Completed patches also support guarded
manual rollback. An explicitly user-approved `--keep-changes` run can
retain partial declared changes after failure and records that decision
distinctly. Snapshot and handoff commands produce bounded context without
dumping source contents. The same completed job ID and normalized hash returns
`ALREADY_APPLIED`; the same ID with different content returns
`PATCH_ID_CONFLICT`. An `UNEXPECTED_WORKSPACE_CHANGE` result means the reported
external path must be reviewed separately; for a normal patch, declared paths
are rolled back. Redaction is best-effort; do not tell the user that a log is
guaranteed to contain no secrets.

Early `VALIDATION_FAILED` and `PLAN_FAILED` logs do not archive invalid source,
update the registry, report project changes, or create backups. Successful
standalone validation and planning do not create logs, and workspace discovery
failures cannot write a log before a workspace is selected.

See `PATCHSHUTTLE_PROTOCOL.md` and `patchshuttle.schema.json` for the detailed
syntax.
