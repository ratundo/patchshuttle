# PatchShuttle release guide

This guide publishes the `0.1.0a2` alpha release candidate through GitHub
Actions and PyPI Trusted Publishing. Do not publish a stable `0.1.0` release
until every acceptance criterion in `SPEC_V0_1.md` has passed.

## 1. Local release gate

From the repository root, use Python 3.12 and run:

```bash
python -m pip install -e ".[dev]"
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

`release_checks.py` requires exactly one wheel and one source archive for the
source version and writes `dist/SHA256SUMS`.

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
   installs exactly `patchshuttle==0.1.0a2` from TestPyPI and runs a clean
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
3. Confirm that the source version is still `0.1.0a2`, the intended tag is
   `v0.1.0a2`, and the changelog is final.
4. Create that Git tag from the exact commit whose CI and TestPyPI workflows
   passed.
5. Create and publish a GitHub pre-release for `v0.1.0a2`.

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
release-check/bin/python -m pip install --pre patchshuttle==0.1.0a2
release-check/bin/patchshuttle version
```

On Windows, use `release-check\Scripts\python.exe` and
`release-check\Scripts\patchshuttle.exe`.

Record links to the GitHub release, CI run, TestPyPI run, production PyPI
project, and post-release smoke result. Only then mark the corresponding
external acceptance gates complete. The phrase `Tested with ChatGPT` requires
a separate recorded ChatGPT end-to-end workflow using the released protocol.
