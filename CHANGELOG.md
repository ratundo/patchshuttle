# Changelog

All notable changes to PatchShuttle are recorded in this file. The project
uses semantic versioning, including Python-compatible pre-release versions.

## Unreleased

## 0.1.0a3 - 2026-08-23

Second alpha release, focused on AI-facing planning diagnostics, legacy-project
formatter compatibility, optional HTML linting, operational self-documentation,
and strict guarded physical-line operations.

### Added

- `patchshuttle plan JOB.psh.yaml --diff` prints a bounded unified preview of
  the fully resolved in-memory file changes without writing project files.
- Exact text-action mismatches now include exact line numbers or up to three
  bounded, similarity-ranked nearby snippets. Unified-diff failures include
  hunk numbers, declared and actual counts, and first context mismatches.
- Changed Python files receive a read-only PEP 263 encoding, isort, and Black
  compatibility preflight during planning.
- The optional `patchshuttle[html]` extra installs djLint. Local configuration
  can enable lint-only checks for the exact changed `.html` scope, including
  isolated stdin-based planning preflight, fixed non-shell execution, log
  records, and automatic rollback on failure. Project djLint configuration
  cannot override this local PatchShuttle policy.
- Validation and planning failures now write timestamped AI-readable attempt
  logs after a workspace is resolved, so `logs --last` exposes the newest
  recorded failure without archiving invalid job source or updating registry
  state.
- A global `--workspace PATH` option routes workspace-aware commands to an
  exact root. Missing implicit workspaces can report bounded direct-child
  candidates without selecting one automatically.
- Workspace-independent `capabilities`, `schema`, and `explain TOPIC` commands
  expose the installed protocol surface without weakening protected paths.
- Local `isort_exclude` and `black_exclude` lists accept exact normalized
  changed-Python paths. Plans and logs expose a per-file, per-tool decision
  matrix while jobs remain unable to change formatter policy.
- `django_import_check` imports bounded dotted module names through
  `manage.py shell -c`, allowing project checks that require initialized
  Django settings and the app registry without accepting arbitrary code.
- Read-only `hash_range` and guarded `replace_range`, `delete_range`, and
  `insert_at_line` operations add 1-based inclusive physical-line addressing
  without weakening identity checks. Content and SHA-256 guards use canonical
  LF/UTF-8 bytes, evaluate against sequential simulated content, and fail
  closed without fuzzy relocation or partial application.

### Changed

- Backup manifests record the approved HTML lint scope, and run logs include a
  fixed `LINT_HTML` section plus `html_lint_status` in the summary.
- HTML linting is disabled by default and cannot be enabled or reconfigured by
  an AI job.
- Formatter preflight distinguishes an incompatible legacy baseline from
  incompatibility introduced by planned content, allows a patch that repairs
  the baseline, and retains bounded Black stderr in planning diagnostics.
- Black compatibility preflight and execution use the same controlled CLI
  policy options; Black's ordinary check-only reformat exit remains compatible.

### Fixed

- Source-version transitions no longer compare a new checkout version with
  stale editable-install metadata before rebuilding. Distribution metadata
  remains validated from the wheel and source archive by the release gates.
- Python checks now redirect bytecode caches to an isolated temporary
  directory, so `compileall`, imports, and test collection do not leave
  `__pycache__` entries that can obstruct rollback of a newly created package.
- A defensive runtime-cache ledger removes only new regular `.pyc` files and
  newly empty `__pycache__` directories in the changed-Python scope before
  completion or rollback, while preserving every pre-existing or foreign
  entry.
- Binary Python targets are classified before PEP 263 formatter preflight, and
  resolved-diff no-change coverage now uses platform-stable LF bytes.
- Symlink-dependent tests now skip consistently when the platform or current
  process cannot create symbolic links.

## 0.1.0a2 - 2026-08-16

First published alpha release, qualified on TestPyPI and released to production
PyPI through Trusted Publishing.

### Added

- Existing-project and empty-directory initialization with stable project IDs.
- Protocol 1 YAML jobs and equivalent immutable Python constructors.
- Bounded `audit`, transactional `patch`, and one-pass `verify` workflows.
- All v0.1 text actions, controlled Python, pytest, unittest, Django, import,
  and locally configured checks.
- Per-job backups, automatic rollback, guarded manual rollback, and explicit
  `--keep-changes` failure handling.
- Changed-Python-only isort followed by Black, with final check reruns.
- Workspace inventory comparison and unexpected side-effect reporting.
- Fixed-section timestamped logs, exact job archives, registry idempotency,
  project snapshots, and compact AI handoffs.
- Integration, end-to-end, wheel smoke, release validation, and index smoke
  tests.
- GitHub Actions workflows for the required Ubuntu and Windows matrix and for
  Trusted Publishing to TestPyPI and PyPI.

### Security

- Deny-by-default confirmation for patch and verify execution.
- Protected and ignored path policy, symbolic-link and special-file rejection,
  bounded inputs and outputs, non-shell subprocess execution, and best-effort
  log redaction.

### Fixed

- Python 3.10 TOML compatibility in package and index-smoke checks.
- Windows inventory capture when path metadata does not expose stable inode
  identifiers.
- Persistent cross-platform workspace locking with `filelock` native locks.
- Platform-neutral subprocess newlines and portable filesystem test fixtures.

### Qualification status

- Local Linux/Python 3.10 and 3.12 verification is complete with 100% statement
  and branch coverage.
- The required GitHub-hosted Ubuntu/Python 3.10, 3.12, and 3.14 plus
  Windows/Python 3.12 and 3.14
  [matrix passed](https://github.com/ratundo/patchshuttle/actions/runs/31969439894)
  on 2026-08-16.
- The
  [TestPyPI workflow](https://github.com/ratundo/patchshuttle/actions/runs/31968375793)
  and its clean index installation passed before production publication.
- The
  [production workflow](https://github.com/ratundo/patchshuttle/actions/runs/31969527644),
  [GitHub pre-release](https://github.com/ratundo/patchshuttle/releases/tag/v0.1.0a2),
  and [PyPI publication](https://pypi.org/project/patchshuttle/0.1.0a2/)
  completed on 2026-08-16, followed by a clean installation smoke test.
- TestPyPI and PyPI received byte-identical files. The wheel SHA-256 is
  `debf664d9ffc1f2763d55fcb0fb4bb47b226ad5b40aa1bf099ce0ab9c6c0c2e6` and
  the source archive SHA-256 is
  `1ae7f233a674704a49f186ef1842e490f10f9814701f712aabed9d16ebe5164c`.
- A recorded ChatGPT audit, patch, formatting, repeated-check, and independent
  verification workflow passed on Windows 11 with Python 3.14.2 on 2026-08-17.
  See [the release record](docs/RELEASE.md#chatgpt-end-to-end-record) for its
  scope and job hashes.

## 0.1.0a1 - 2026-08-06

Internal development checkpoint containing the initial package scaffold,
typed job model, validation, workspace, path policy, planner, and early
transaction engine. It was not published as a supported release.
