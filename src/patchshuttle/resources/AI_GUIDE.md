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

## Local workflow

The user saves the YAML in `patches/inbox/`, validates it, runs
`patchshuttle plan`, and reviews the read-only plan locally. An audit can run
without confirmation. Patch and verify jobs require explicit confirmation or
deliberate `--yes` automation. If validation or planning fails, ask for the
exact terminal result. After every recorded attempt, ask the user for the
generated `.log` file or a fresh `patchshuttle handoff` file and use its final
`PATCHSHUTTLE_AI_HANDOFF` block before preparing the next job. Never state that
a job was applied merely because YAML or a successful plan was created.

Interpret failure states conservatively:

- `ROLLED_BACK` means declared transaction paths were restored, but reported
  external check side effects still require review.
- `SKIPPED_CHANGES_KEPT` means the user explicitly retained partial declared
  changes. Request a fresh audit or handoff before producing a corrective job.
- `SKIPPED_NO_CHANGES` means rollback was skipped before a declared change was
  published; still use the log as the authoritative result.
- `ROLLBACK_FAILED` means restoration is incomplete. Stop proposing new
  changes until the user resolves the listed paths.

This `0.1.0a2` build provides workspace initialization, validation, read-only
planning, bounded audit execution, approved patch execution, and approved
one-pass verification. Patch execution uses all text change actions,
controlled initial checks, changed-Python-only isort then Black, final checks,
bounded before/after workspace inventory, undeclared side-effect reporting,
rollback of declared transaction paths, exact CLI job archives, an atomic
registry, and fixed-section redacted logs. Completed patches also support
guarded manual rollback. An explicitly user-approved `--keep-changes` run can
retain partial declared changes after failure and records that decision
distinctly. Snapshot and handoff commands produce bounded context without
dumping source contents. The same completed job ID and normalized hash returns
`ALREADY_APPLIED`; the same ID with different content returns
`PATCH_ID_CONFLICT`. An `UNEXPECTED_WORKSPACE_CHANGE` result means the reported
external path must be reviewed separately; for a normal patch, declared paths
are rolled back. Redaction is best-effort; do not tell the user that a log is
guaranteed to contain no secrets.

See `PATCHSHUTTLE_PROTOCOL.md` and `patchshuttle.schema.json` for the detailed
syntax.
