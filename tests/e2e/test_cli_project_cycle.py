"""Cross-platform CLI cycle starting from an empty directory."""

from __future__ import annotations

import os
import subprocess
import sysconfig
from pathlib import Path

import yaml


def _cli_executable() -> Path:
    suffix = ".exe" if os.name == "nt" else ""
    executable = Path(sysconfig.get_path("scripts")) / f"patchshuttle{suffix}"
    assert executable.is_file()
    return executable


def _run(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [str(_cli_executable()), *arguments],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result


def _project_id(root: Path) -> str:
    for line in (root / "patches/patchshuttle.toml").read_text("utf-8").splitlines():
        if line.startswith("project_id = "):
            return line.split('"', 2)[1]
    raise AssertionError("project ID was not generated")


def _write_job(root: Path, payload: dict) -> Path:
    path = root / "patches/inbox" / f"{payload['id']}.psh.yaml"
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


def _patch_payload(
    project_id: str,
    job_id: str,
    action: dict,
) -> dict:
    return {
        "protocol": 1,
        "project_id": project_id,
        "id": job_id,
        "kind": "patch",
        "actions": [action],
        "checks": [{"compileall": {"paths": ["app.py"]}}],
    }


def test_three_jobs_audit_verify_handoff_and_rollback(tmp_path: Path) -> None:
    _run(tmp_path, "init", "--new-project")
    project_id = _project_id(tmp_path)

    first = _write_job(
        tmp_path,
        _patch_payload(
            project_id,
            "E2E-PATCH-001",
            {
                "create_file": {
                    "path": "app.py",
                    "content": "def answer():\n return 40\n",
                }
            },
        ),
    )
    _run(tmp_path, "run", str(first.relative_to(tmp_path)), "--yes")
    assert "return 40" in (tmp_path / "app.py").read_text("utf-8")

    second = _write_job(
        tmp_path,
        _patch_payload(
            project_id,
            "E2E-PATCH-002",
            {
                "replace_exact": {
                    "path": "app.py",
                    "old": "return 40",
                    "new": "return 41",
                }
            },
        ),
    )
    _run(tmp_path, "run", str(second.relative_to(tmp_path)), "--yes")
    handoff = _run(tmp_path, "handoff")
    handoff_path = Path(handoff.stdout.split("log: ", 1)[1].splitlines()[0])
    assert "E2E-PATCH-002" in handoff_path.read_text("utf-8")

    third = _write_job(
        tmp_path,
        _patch_payload(
            project_id,
            "E2E-PATCH-003",
            {
                "replace_exact": {
                    "path": "app.py",
                    "old": "return 41",
                    "new": "return 42",
                }
            },
        ),
    )
    _run(tmp_path, "run", str(third.relative_to(tmp_path)), "--yes")
    assert "return 42" in (tmp_path / "app.py").read_text("utf-8")

    audit = _write_job(
        tmp_path,
        {
            "protocol": 1,
            "project_id": project_id,
            "id": "E2E-AUDIT-001",
            "kind": "audit",
            "actions": [
                {"search": {"path": "app.py", "text": "return 42"}},
                {"hash": {"path": "app.py"}},
            ],
        },
    )
    audited = _run(tmp_path, "audit", str(audit.relative_to(tmp_path)))
    assert "audit_results: 2" in audited.stdout

    verify = _write_job(
        tmp_path,
        {
            "protocol": 1,
            "project_id": project_id,
            "id": "E2E-VERIFY-001",
            "kind": "verify",
            "checks": [{"compileall": {"paths": ["app.py"]}}],
        },
    )
    verified = _run(
        tmp_path,
        "verify",
        str(verify.relative_to(tmp_path)),
        "--yes",
    )
    assert "initial_checks: 1" in verified.stdout
    _run(tmp_path, "snapshot")
    status = _run(tmp_path, "status")
    assert "E2E-PATCH-003" in status.stdout

    rolled_back = _run(tmp_path, "rollback", "E2E-PATCH-003", "--yes")
    assert rolled_back.stdout.startswith("ROLLED_BACK\n")
    assert "return 41" in (tmp_path / "app.py").read_text("utf-8")
    assert len(tuple((tmp_path / "patches/logs").glob("log_*.log"))) >= 8
