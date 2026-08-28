"""Install one wheel in a clean environment and exercise the public workflow."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import venv
from pathlib import Path

_WORKFLOW = r"""
from importlib.metadata import version
from pathlib import Path

from patchshuttle import (
    Job,
    create_handoff,
    create_snapshot,
    execute_plan,
    init_workspace,
    load_workspace,
    plan_job,
    rollback_job,
)
from patchshuttle.actions import create_file, hash, hash_range, replace_range
from patchshuttle.checks import compileall, django_import_check
from patchshuttle.history import HISTORY_SCHEMA_VERSION, latest_history_record

root = Path.cwd() / "project"
root.mkdir()
workspace = init_workspace(root, new_project=True).workspace
workspace.config_path.write_text(
    workspace.config_path.read_text("utf-8")
    .replace("enabled = false", "enabled = true", 1)
    .replace("black_exclude = []", 'black_exclude = ["legacy.py"]'),
    encoding="utf-8",
)
workspace = load_workspace(root)
assert django_import_check(("app.models",)).name == "django_import_check"

html = Job(
    protocol=1,
    project_id=workspace.project_id,
    id="SMOKE-HTML",
    kind="patch",
    actions=(
        create_file(
            "templates/page.html",
            '<!doctype html>\n<html lang="en">\n'
            "  <head>\n"
            '    <meta name="description" content="Example">\n'
            "    <title>Example</title>\n"
            "  </head>\n"
            "  <body>\n"
            "    <main>ok</main>\n"
            "  </body>\n"
            "</html>\n",
        ),
    ),
    checks=(compileall(("templates",)),),
)
html_result = execute_plan(plan_job(html, workspace), approved=True)
assert html_result.status.value == "COMPLETED"
assert html_result.html_lint_results[0].status.value == "PASSED"

patch = Job(
    protocol=1,
    project_id=workspace.project_id,
    id="SMOKE-PATCH",
    kind="patch",
    actions=(create_file("hello.py", "VALUE=1\n"),),
    checks=(compileall(("hello.py",)),),
)
patched = execute_plan(plan_job(patch, workspace), approved=True)
assert patched.status.value == "COMPLETED"
assert (root / "hello.py").read_text("utf-8") == "VALUE = 1\n"
patch_history = latest_history_record(workspace, job_id=patch.id)
assert patch_history.schema_version == HISTORY_SCHEMA_VERSION == 1
assert patch_history.job.id == patch.id
assert patch_history.observed.status == "COMPLETED"
assert patch_history.references.detailed_log.endswith("SMOKE-PATCH.log")

legacy = Job(
    protocol=1,
    project_id=workspace.project_id,
    id="SMOKE-LEGACY",
    kind="patch",
    actions=(create_file("legacy.py", "VALUE=2\n"),),
    checks=(compileall(("legacy.py",)),),
)
legacy_result = execute_plan(plan_job(legacy, workspace), approved=True)
assert [item.name for item in legacy_result.formatting_results] == ["isort"]
assert (root / "legacy.py").read_text("utf-8") == "VALUE=2\n"

audit = Job(
    protocol=1,
    project_id=workspace.project_id,
    id="SMOKE-AUDIT",
    kind="audit",
    actions=(hash("hello.py"),),
)
audited = execute_plan(plan_job(audit, workspace))
assert audited.audit_results[0].status == "COMPLETED"

range_audit = Job(
    protocol=1,
    project_id=workspace.project_id,
    id="SMOKE-RANGE-AUDIT",
    kind="audit",
    actions=(hash_range("hello.py", 1, 1),),
)
range_audited = execute_plan(plan_job(range_audit, workspace))
range_digest = next(
    line.removeprefix("sha256: ")
    for line in range_audited.audit_results[0].output.splitlines()
    if line.startswith("sha256: ")
)
range_patch = Job(
    protocol=1,
    project_id=workspace.project_id,
    id="SMOKE-RANGE-PATCH",
    kind="patch",
    actions=(
        replace_range(
            "hello.py",
            1,
            1,
            "VALUE = 3\n",
            expected_sha256=range_digest,
        ),
    ),
    checks=(compileall(("hello.py",)),),
)
range_patched = execute_plan(plan_job(range_patch, workspace), approved=True)
assert range_patched.status.value == "COMPLETED"
assert (root / "hello.py").read_text("utf-8") == "VALUE = 3\n"

verify = Job(
    protocol=1,
    project_id=workspace.project_id,
    id="SMOKE-VERIFY",
    kind="verify",
    checks=(compileall(("hello.py",)),),
)
verified = execute_plan(plan_job(verify, workspace), approved=True)
assert verified.status.value == "COMPLETED"
assert create_snapshot(workspace).path.is_file()
assert create_handoff(workspace).path.is_file()

range_rolled_back = rollback_job(workspace, range_patch.id, approved=True)
assert range_rolled_back.job_id == range_patch.id
assert (root / "hello.py").read_text("utf-8") == "VALUE = 1\n"
rolled_back = rollback_job(workspace, patch.id, approved=True)
assert rolled_back.job_id == patch.id
assert not (root / "hello.py").exists()
legacy_rollback = rollback_job(workspace, legacy.id, approved=True)
assert legacy_rollback.job_id == legacy.id
assert not (root / "legacy.py").exists()
html_rollback = rollback_job(workspace, html.id, approved=True)
assert html_rollback.job_id == html.id
assert not (root / "templates/page.html").exists()
assert version("patchshuttle") == EXPECTED_VERSION
"""


def _environment_python(directory: Path) -> Path:
    if os.name == "nt":
        return directory / "Scripts/python.exe"
    return directory / "bin/python"


def _environment_cli(directory: Path) -> Path:
    if os.name == "nt":
        return directory / "Scripts/patchshuttle.exe"
    return directory / "bin/patchshuttle"


def _run(argv: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv,
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except subprocess.CalledProcessError as exc:
        if exc.stdout:
            sys.stderr.write(exc.stdout)
        if exc.stderr:
            sys.stderr.write(exc.stderr)
        raise


def smoke_wheel(wheel: Path, expected_version: str) -> None:
    """Install and exercise one wheel outside the source checkout."""

    wheel = wheel.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="patchshuttle-wheel-smoke-") as raw:
        root = Path(raw)
        environment = root / "venv"
        venv.EnvBuilder(with_pip=True, clear=True).create(environment)
        python = _environment_python(environment)
        cli = _environment_cli(environment)
        _run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-input",
                str(wheel),
            ],
            cwd=root,
        )
        _run([str(python), "-m", "pip", "check"], cwd=root)
        _run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-input",
                f"{wheel}[html]",
            ],
            cwd=root,
        )
        _run([str(python), "-m", "pip", "check"], cwd=root)
        reported = _run([str(cli), "version"], cwd=root).stdout.strip()
        if reported != f"PatchShuttle {expected_version}":
            raise RuntimeError(f"unexpected CLI version output: {reported!r}")
        capabilities = _run([str(cli), "capabilities"], cwd=root).stdout
        if not all(
            expected in capabilities
            for expected in (
                "arbitrary_shell_action: unavailable",
                "hash_range",
                "replace_range",
            )
        ):
            raise RuntimeError("installed capability output is incomplete")
        schema = json.loads(_run([str(cli), "schema"], cwd=root).stdout)
        if schema.get("title") != "Job":
            raise RuntimeError("installed schema output is invalid")
        explanation = _run(
            [str(cli), "explain", "replace_range"],
            cwd=root,
        ).stdout
        if "topic: replace_range" not in explanation:
            raise RuntimeError("installed explanation output is invalid")
        program = "EXPECTED_VERSION = " + repr(expected_version) + "\n" + _WORKFLOW
        _run([str(python), "-c", textwrap.dedent(program)], cwd=root)
        routed = _run(
            [str(cli), "--workspace", str(root / "project"), "status"],
            cwd=root,
        ).stdout
        if not routed.startswith("STATUS\n"):
            raise RuntimeError("explicit workspace routing failed")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("wheel", type=Path)
    parser.add_argument("--version", required=True)
    arguments = parser.parse_args()
    try:
        smoke_wheel(arguments.wheel, arguments.version)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"WHEEL_SMOKE_FAILED: {exc}\n")
    print(f"WHEEL_SMOKE_OK {arguments.version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
