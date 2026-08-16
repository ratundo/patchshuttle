"""Install an exact PatchShuttle release from PyPI or TestPyPI."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
import venv
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib  # type: ignore[no-redef]

_INDEXES = {
    "pypi": "https://pypi.org/simple/",
    "testpypi": "https://test.pypi.org/simple/",
}

_SMOKE = r"""
from importlib.metadata import version
from pathlib import Path

from patchshuttle import Job, execute_plan, init_workspace, plan_job
from patchshuttle.actions import environment

assert version("patchshuttle") == EXPECTED_VERSION
root = Path.cwd() / "index-project"
root.mkdir()
workspace = init_workspace(root, new_project=True).workspace
job = Job(
    protocol=1,
    project_id=workspace.project_id,
    id="INDEX-AUDIT",
    kind="audit",
    actions=(environment(),),
)
result = execute_plan(plan_job(job, workspace))
assert result.audit_results[0].status == "COMPLETED"
assert result.log_path is not None and result.log_path.is_file()
"""


def _environment_python(directory: Path) -> Path:
    if os.name == "nt":
        return directory / "Scripts/python.exe"
    return directory / "bin/python"


def _run(argv: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        timeout=300,
    )


def _install_release(
    python: Path,
    root: Path,
    *,
    repository: str,
    version: str,
    attempts: int,
    delay_seconds: int,
) -> None:
    index = _INDEXES[repository]
    command = [
        str(python),
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-input",
        "--no-deps",
        "--index-url",
        index,
        f"patchshuttle=={version}",
    ]
    last_error: subprocess.CalledProcessError | None = None
    for attempt in range(1, attempts + 1):
        try:
            _run(command, cwd=root)
            return
        except subprocess.CalledProcessError as exc:
            last_error = exc
            if attempt < attempts:
                print(f"index package not ready, retry {attempt}/{attempts}")
                time.sleep(delay_seconds)
    assert last_error is not None
    if last_error.stdout:
        sys.stderr.write(last_error.stdout)
    if last_error.stderr:
        sys.stderr.write(last_error.stderr)
    raise last_error


def smoke_index(
    project_root: Path,
    *,
    repository: str,
    version: str,
    attempts: int,
    delay_seconds: int,
) -> None:
    configuration = tomllib.loads((project_root / "pyproject.toml").read_text("utf-8"))
    dependencies = configuration["project"]["dependencies"]
    with tempfile.TemporaryDirectory(prefix="patchshuttle-index-smoke-") as raw:
        root = Path(raw)
        environment = root / "venv"
        venv.EnvBuilder(with_pip=True, clear=True).create(environment)
        python = _environment_python(environment)
        _run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-input",
                *dependencies,
            ],
            cwd=root,
        )
        _install_release(
            python,
            root,
            repository=repository,
            version=version,
            attempts=attempts,
            delay_seconds=delay_seconds,
        )
        _run([str(python), "-m", "pip", "check"], cwd=root)
        program = "EXPECTED_VERSION = " + repr(version) + "\n" + _SMOKE
        _run([str(python), "-c", program], cwd=root)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("repository", choices=sorted(_INDEXES))
    parser.add_argument("--version", required=True)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--attempts", type=int, default=12)
    parser.add_argument("--delay-seconds", type=int, default=10)
    arguments = parser.parse_args()
    if arguments.attempts < 1 or arguments.delay_seconds < 0:
        parser.error("attempts must be positive and delay-seconds cannot be negative")
    try:
        smoke_index(
            arguments.root.resolve(),
            repository=arguments.repository,
            version=arguments.version,
            attempts=arguments.attempts,
            delay_seconds=arguments.delay_seconds,
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"INDEX_SMOKE_FAILED: {exc}\n")
    print(f"INDEX_SMOKE_OK {arguments.repository} {arguments.version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
