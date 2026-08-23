# PatchShuttle release guide

This guide records the immutable published `0.1.0a2` and `0.1.0a3` evidence and
the staged qualification procedure for later releases. Do not publish a
stable `0.1.0` release until every acceptance criterion in `SPEC_V0_1.md` has
passed and the stable release is approved.

## 0. Recorded `0.1.0a2` qualification

The immutable `0.1.0a2` artifacts were built from commit
`29565522c2c65fa0ecfb9234403e6220d1721c37` and passed these external gates:

- [required GitHub-hosted CI matrix](https://github.com/ratundo/patchshuttle/actions/runs/31969439894);
- [TestPyPI publication and clean index installation](https://github.com/ratundo/patchshuttle/actions/runs/31968375793);
- [GitHub `v0.1.0a2` pre-release](https://github.com/ratundo/patchshuttle/releases/tag/v0.1.0a2);
- [production PyPI Trusted Publishing workflow](https://github.com/ratundo/patchshuttle/actions/runs/31969527644);
- [production PyPI project](https://pypi.org/project/patchshuttle/0.1.0a2/)
  and a clean post-release installation smoke test.

TestPyPI and production PyPI received byte-identical distributions:

- wheel `patchshuttle-0.1.0a2-py3-none-any.whl` SHA-256:
  `debf664d9ffc1f2763d55fcb0fb4bb47b226ad5b40aa1bf099ce0ab9c6c0c2e6`;
- source archive `patchshuttle-0.1.0a2.tar.gz` SHA-256:
  `1ae7f233a674704a49f186ef1842e490f10f9814701f712aabed9d16ebe5164c`.

### ChatGPT end-to-end record

On 2026-08-17, a Windows 11 workspace running CPython 3.14.2 and the published
PatchShuttle 0.1.0a2 package completed this ChatGPT-guided sequence:

- `AUDIT-001`, job SHA-256
  `c773d787275a11bae2832172782d554caf3b4e1b3bad856715e00d195ce79488`:
  bounded inspection completed without workspace changes;
- `PATCH-001`, job SHA-256
  `17a0a4996cfd796649f9efa4fe03b288658d35a7d636bf56e88a286e7e0a34c7`:
  created a small Python module, three unit tests, and project documentation;
  compileall, unittest, and import checks passed before and after scoped isort
  and Black formatting;
- `VERIFY-001`, job SHA-256
  `2e2ead1826f80044190d55c4a07f212b789d9b5b7aa57b21a9143ab4e88b25eb`:
  independently repeated compileall, the three unit tests, and the import
  check without declared changes.

All three jobs completed with passing workspace comparisons. This is the
evidence behind **Tested with ChatGPT** for the recorded audit, patch,
formatting, repeated-check, and independent verification workflow. It does not
cover every supported action, intentional failure or rollback path, AI model,
or project.

## 0.1. Recorded `0.1.0a3` qualification

The `0.1.0a3` source version and changelog were prepared on 2026-08-23 after
the feature-complete commit `d588abf` passed the
[required GitHub-hosted matrix and distribution smoke job](https://github.com/ratundo/patchshuttle/actions/runs/32650889118).
The candidate adds planner diagnostics and previews, formatter preflight and
local exclusions, optional HTML linting, failure-attempt logs, explicit
workspace routing, installed self-documentation, Django-aware imports,
defensive runtime-cache cleanup, and guarded physical-line operations.

A bounded `AUDIT-A3-RELEASE-001` run on Windows 11 with CPython 3.14.2 inspected
version metadata, release documentation, workflows, and distribution smoke
scripts. All 23 read-only actions completed with a passing workspace comparison
and zero project changes. Its normalized job hash was
`da1c4be9ad9846cc35ffb20f0f9a71fbb5502721459c8d791f7e93740c9ea856`.

The immutable `0.1.0a3` artifacts were built from commit
`06f3125aacd92ae32a832d34d86246a97fdc74f7` and passed these external gates on
2026-08-23:

- [final release-candidate CI on `main`](https://github.com/ratundo/patchshuttle/actions/runs/32654653981);
- [TestPyPI publication and clean-index verification](https://github.com/ratundo/patchshuttle/actions/runs/32655312241);
- [CI for tag `v0.1.0a3`](https://github.com/ratundo/patchshuttle/actions/runs/32655767001);
- [GitHub `v0.1.0a3` pre-release](https://github.com/ratundo/patchshuttle/releases/tag/v0.1.0a3);
- [production PyPI Trusted Publishing and verification](https://github.com/ratundo/patchshuttle/actions/runs/32656270890);
- [production PyPI release](https://pypi.org/project/patchshuttle/0.1.0a3/)
  and a clean post-release installation smoke test.

The first production publish attempt was rejected before runner allocation
because the protected `pypi` environment did not yet allow tag references.
After the selected-tag rule `v*` was added, rerunning the failed jobs published
and verified the release successfully.

TestPyPI and production PyPI received byte-identical distributions:

- wheel `patchshuttle-0.1.0a3-py3-none-any.whl`, 136976 bytes, SHA-256:
  `381ef149b4444b48ca283c679623e22395bc3528296a0ec6c5f897fbf308f981`;
- source archive `patchshuttle-0.1.0a3.tar.gz`, 243110 bytes, SHA-256:
  `5b943a7388f59bc615d8b47644591c6c3540c3e777603dd2dea5f34d3ac1a9f6`.

A separate clean Windows CPython 3.14 temporary environment installed
`patchshuttle[html]==0.1.0a3` from production PyPI with `--no-cache-dir`.
`patchshuttle version`, `patchshuttle capabilities`,
`patchshuttle explain replace_range`, and installed package metadata all
reported `0.1.0a3` successfully.

The **Tested with ChatGPT** statement remains scoped to the separately recorded
`0.1.0a2` end-to-end workflow above; this release record does not silently
extend that product claim to `0.1.0a3`.

## 1. Local release gate

From a clean repository root, use Python 3.12 and start with an empty `dist/`
directory so immutable artifacts from an older version cannot be mixed with
the candidate. Then run:

```bash
python -m pip install -e ".[dev,html]"
python -m isort --check-only src tests tools
python -m black --check src tests tools
python -m coverage erase
python -m coverage run -m pytest -q
python -m coverage report --fail-under=100
python -m build
python -m twine check dist/*
python tools/release_checks.py dist
python tools/wheel_smoke.py dist/patchshuttle-<VERSION>-py3-none-any.whl --version <VERSION>
```

`release_checks.py` requires exactly one wheel and one source archive for the
source version and writes `dist/SHA256SUMS`. Replace `<VERSION>` with the exact
source version under qualification. For the recorded qualification above it
was `0.1.0a3`. Do not reuse an immutable published version for a future upload.

During a source-version transition, metadata in an already-installed editable
environment may still report the previous version until the package is
reinstalled. Source-tree tests validate the imported source version. The build,
`release_checks.py`, and clean-wheel smoke test are the authoritative metadata
alignment gates for the new distributions.

For the next alpha, the local and hosted suites must also retain explicit
coverage for formatter baseline-versus-planned classification, per-tool local
exclusions, `django_import_check`, and rollback after runtime `.pyc` creation.
The plan and run logs must show the resolved formatter decision for each
changed Python path.

## 2. Publish the repository and run CI

1. Create a public GitHub repository named `patchshuttle`.
2. Upload the release-candidate source tree, including `.github/`, `docs/`,
   `tools/`, tests, license, changelog, security policy, and specification.
3. Do not upload `.venv/`, caches, previous phase archives, or local runtime
   `patches/` directories.
4. Push the default branch and wait for the `CI` workflow.
5. Require all five matrix jobs and the package job to pass before publishing.

The required matrix is Ubuntu/Python 3.10, 3.12, and 3.14 plus
Windows/Python 3.12 and 3.14. A local Linux result does not replace these jobs.

## 3. Configure TestPyPI Trusted Publishing

1. In the GitHub repository, create an Actions environment named `testpypi`.
2. In the TestPyPI account publishing settings, add a pending trusted
   publisher for:

   - PyPI project name: `patchshuttle`
   - GitHub owner: the repository owner's exact account or organization
   - Repository: `patchshuttle`
   - Workflow: `testpypi.yml`
   - Environment: `testpypi`

3. In GitHub Actions, manually run **Publish to TestPyPI** from the verified
   commit.
4. Require its `build`, `publish`, and `verify` jobs to pass. The final job
   installs exactly `patchshuttle==<VERSION>` from TestPyPI and runs a clean
   smoke test.

No API token or repository secret is needed. Only the isolated `publish` job
receives `id-token: write`.

## 4. Configure production PyPI

After TestPyPI succeeds:

1. Create a GitHub Actions environment named `pypi` and add appropriate
   reviewer protection if the account supports it.
2. In the production PyPI publishing settings, add a pending trusted publisher
   with workflow `release.yml` and environment `pypi`; the other project and
   repository values are the same as above.
3. Confirm that the source version is still `<VERSION>`, the intended tag is
   `v<VERSION>`, and the changelog is final.
4. Create that Git tag from the exact commit whose CI and TestPyPI workflows
   passed.
5. Create and publish a GitHub pre-release for `v<VERSION>`.

Publishing the GitHub release starts **Release to PyPI**. Its build job checks
that the tag matches the source version, reruns the test and package gates,
and smoke-tests the wheel. The isolated publish job then uses Trusted
Publishing, and the final job installs the exact release from production PyPI.

PyPI files and versions are immutable. Never replace an uploaded artifact or
reuse a version after a faulty or partial upload. Correct the problem, bump the
pre-release version, rebuild from a clean `dist/`, and run every gate again.

## 5. Post-release verification

Confirm all workflow jobs are green, then run in a new local environment:

```bash
python -m venv release-check
release-check/bin/python -m pip install --pre patchshuttle==<VERSION>
release-check/bin/patchshuttle version
```

On Windows, use `release-check\Scripts\python.exe` and
`release-check\Scripts\patchshuttle.exe`.

Record links to the GitHub release, CI run, TestPyPI run, production PyPI
project, and post-release smoke result. Only then mark the corresponding
external acceptance gates complete. Section 0 contains that record for
`0.1.0a2`; future releases require their own evidence. A `Tested with ChatGPT`
statement also requires a separate recorded ChatGPT end-to-end workflow using
that released protocol and must state the tested scope.
