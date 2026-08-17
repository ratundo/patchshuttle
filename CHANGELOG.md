# Changelog

All notable changes to PatchShuttle are recorded in this file. The project
uses semantic versioning, including Python-compatible pre-release versions.

## Unreleased

### Fixed

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
