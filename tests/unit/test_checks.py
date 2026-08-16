"""Contract tests for controlled project-check execution."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import time
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

import patchshuttle.checks.runner as checks_module
import patchshuttle.planner as planner_module
import patchshuttle.workspace as workspace_module
from patchshuttle import Job
from patchshuttle import _process as process_module
from patchshuttle.checks import CheckStatus, prepare_checks, run_checks
from patchshuttle.config import CheckProfileSettings, ChecksSettings
from patchshuttle.planner import Plan, plan_job
from patchshuttle.workspace import Workspace, init_workspace

PROJECT_ID = "PSH-8F41C2A73D905E61"


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Workspace:
    monkeypatch.setattr(
        workspace_module,
        "generate_project_id",
        lambda: PROJECT_ID,
    )
    return init_workspace(tmp_path).workspace


def configured_workspace(
    workspace: Workspace,
    *,
    profiles: dict[str, CheckProfileSettings] | None = None,
    default_timeout: int = 300,
    max_output: int = 2_000_000,
) -> Workspace:
    execution = workspace.config.execution.model_copy(
        update={
            "default_timeout_seconds": default_timeout,
            "max_command_output_bytes": max_output,
        }
    )
    checks = ChecksSettings(profiles=profiles or {})
    config = workspace.config.model_copy(
        update={"execution": execution, "checks": checks}
    )
    return replace(workspace, config=config)


def verify_plan(workspace: Workspace, checks: list[dict]) -> Plan:
    job = Job(
        protocol=1,
        project_id=PROJECT_ID,
        id="VERIFY-010",
        kind="verify",
        checks=checks,
    )
    return plan_job(job, workspace)


def test_prepare_checks_builds_fixed_commands_for_every_profile(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (workspace.root / "src").mkdir()
    (workspace.root / "src/module.py").write_text("VALUE = 1\n", encoding="utf-8")
    (workspace.root / "tests").mkdir()
    (workspace.root / "manage.py").write_text("pass\n", encoding="utf-8")
    profile = CheckProfileSettings(
        argv=("{python}", "-c", "print('local')"),
        timeout_seconds=19,
    )
    workspace = configured_workspace(
        workspace,
        profiles={"local": profile},
        default_timeout=23,
    )
    real_find_spec = importlib.util.find_spec

    def available_module(name: str):
        if name == "django":
            return object()
        return real_find_spec(name)

    monkeypatch.setattr(planner_module, "find_spec", available_module)
    plan = verify_plan(
        workspace,
        [
            {"compileall": {"paths": ["src/module.py"], "quiet": 2}},
            {
                "pytest": {
                    "paths": ["tests"],
                    "args": ["-q", "--maxfail=2"],
                    "timeout_seconds": 17,
                }
            },
            {"unittest": {"discover": "tests", "pattern": "test_*.py"}},
            {"django_check": {"manage_py": "manage.py"}},
            {"django_migrations_check": {"manage_py": "manage.py"}},
            {
                "django_test": {
                    "manage_py": "manage.py",
                    "labels": ["clients.tests"],
                }
            },
            {"import_check": {"modules": ["json", "pathlib"]}},
            {"profile": {"name": "local"}},
        ],
    )

    prepared = prepare_checks(plan)

    assert [item.id for item in prepared] == [
        f"check_{index:03d}" for index in range(1, 9)
    ]
    assert [item.name for item in prepared] == [
        "compileall",
        "pytest",
        "unittest",
        "django_check",
        "django_migrations_check",
        "django_test",
        "import_check",
        "profile",
    ]
    assert all(item.working_directory == workspace.root for item in prepared)
    assert prepared[0].argv == (
        sys.executable,
        "-m",
        "compileall",
        "-qq",
        "--",
        "src/module.py",
    )
    assert prepared[1].argv == (
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "--maxfail=2",
        "--",
        "tests",
    )
    assert prepared[2].argv == (
        sys.executable,
        "-m",
        "unittest",
        "discover",
        "-s",
        "tests",
        "-p",
        "test_*.py",
    )
    assert prepared[3].argv == (sys.executable, "manage.py", "check")
    assert prepared[4].argv == (
        sys.executable,
        "manage.py",
        "makemigrations",
        "--check",
        "--dry-run",
    )
    assert prepared[5].argv == (
        sys.executable,
        "manage.py",
        "test",
        "clients.tests",
    )
    assert prepared[6].argv[:2] == (sys.executable, "-c")
    assert prepared[6].argv[-2:] == ("json", "pathlib")
    assert prepared[7].argv == (sys.executable, "-c", "print('local')")
    assert [item.timeout_seconds for item in prepared] == [
        23,
        17,
        23,
        23,
        23,
        23,
        23,
        19,
    ]


def test_run_checks_executes_successful_checks_in_order(
    workspace: Workspace,
) -> None:
    (workspace.root / "src").mkdir()
    (workspace.root / "src/good.py").write_text("VALUE = 1\n", encoding="utf-8")
    plan = verify_plan(
        workspace,
        [
            {"compileall": {"paths": ["src/good.py"], "quiet": 2}},
            {"import_check": {"modules": ["json"]}},
        ],
    )

    run = run_checks(plan)

    assert run.success is True
    assert run.failed is None
    assert [result.status for result in run.results] == [
        CheckStatus.PASSED,
        CheckStatus.PASSED,
    ]
    assert [result.id for result in run.results] == ["check_001", "check_002"]
    assert all(result.duration_ms >= 0 for result in run.results)
    with pytest.raises(FrozenInstanceError):
        run.results[0].status = CheckStatus.FAILED  # type: ignore[misc]


def test_run_checks_stops_after_first_nonzero_exit_and_captures_streams(
    workspace: Workspace,
) -> None:
    marker = workspace.root / "second-ran.txt"
    profiles = {
        "fail": CheckProfileSettings(
            argv=(
                "{python}",
                "-c",
                "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)",
            )
        ),
        "second": CheckProfileSettings(
            argv=(
                "{python}",
                "-c",
                f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')",
            )
        ),
    }
    workspace = configured_workspace(workspace, profiles=profiles)
    plan = verify_plan(
        workspace,
        [
            {"profile": {"name": "fail"}},
            {"profile": {"name": "second"}},
        ],
    )

    run = run_checks(plan)

    assert run.success is False
    assert len(run.results) == 1
    assert run.failed is run.results[0]
    assert run.failed.status is CheckStatus.FAILED
    assert run.failed.return_code == 3
    assert run.failed.stdout == "out\n"
    assert run.failed.stderr == "err\n"
    assert not marker.exists()


def test_run_checks_truncates_each_captured_stream_to_local_limit(
    workspace: Workspace,
) -> None:
    profile = CheckProfileSettings(
        argv=(
            "{python}",
            "-c",
            "import os; os.write(1, b'abcdef'); os.write(2, b'uvwxyz')",
        )
    )
    workspace = configured_workspace(
        workspace,
        profiles={"output": profile},
        max_output=4,
    )
    plan = verify_plan(workspace, [{"profile": {"name": "output"}}])

    result = run_checks(plan).results[0]

    assert result.status is CheckStatus.PASSED
    assert result.stdout == "abcd"
    assert result.stderr == "uvwx"
    assert result.stdout_truncated is True
    assert result.stderr_truncated is True


def test_run_checks_times_out_and_terminates_the_process(
    workspace: Workspace,
) -> None:
    profile = CheckProfileSettings(
        argv=("{python}", "-c", "import time; time.sleep(5)"),
        timeout_seconds=1,
    )
    workspace = configured_workspace(workspace, profiles={"slow": profile})
    plan = verify_plan(workspace, [{"profile": {"name": "slow"}}])
    started = time.monotonic()

    run = run_checks(plan)

    assert time.monotonic() - started < 4
    assert run.success is False
    assert run.failed is not None
    assert run.failed.status is CheckStatus.TIMED_OUT
    assert run.failed.return_code is None


def test_run_checks_maps_process_start_failure(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = verify_plan(workspace, [{"import_check": {"modules": ["json"]}}])
    monkeypatch.setattr(
        checks_module.subprocess,
        "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            FileNotFoundError("missing executable")
        ),
    )

    run = run_checks(plan)

    assert run.success is False
    assert run.failed is not None
    assert run.failed.status is CheckStatus.ERROR
    assert run.failed.return_code is None
    assert "missing executable" in run.failed.stderr


def test_run_checks_uses_workspace_cwd_stdin_null_and_no_shell(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = verify_plan(workspace, [{"import_check": {"modules": ["json"]}}])
    observed: dict[str, object] = {}

    class CompletedProcess:
        pid = 12345

        def wait(self, *, timeout=None):
            return 0

        def poll(self):
            return 0

    def fake_popen(argv, **kwargs):
        observed["argv"] = tuple(argv)
        observed.update(kwargs)
        return CompletedProcess()

    monkeypatch.setattr(checks_module.subprocess, "Popen", fake_popen)

    result = run_checks(plan).results[0]

    assert result.status is CheckStatus.PASSED
    assert observed["argv"] == prepare_checks(plan)[0].argv
    assert observed["cwd"] == workspace.root
    assert observed["stdin"] is subprocess.DEVNULL
    assert observed["shell"] is False


def test_prepare_checks_rejects_a_forged_plan_with_missing_check_record(
    workspace: Workspace,
) -> None:
    plan = verify_plan(workspace, [{"import_check": {"modules": ["json"]}}])
    forged = replace(plan, checks=())

    with pytest.raises(ValueError, match="check records"):
        prepare_checks(forged)


def test_prepare_checks_rejects_forged_record_identity_and_required_path(
    workspace: Workspace,
) -> None:
    import_plan = verify_plan(
        workspace,
        [{"import_check": {"modules": ["json"]}}],
    )
    wrong_identity = replace(
        import_plan,
        checks=(replace(import_plan.checks[0], id="check_999"),),
    )
    with pytest.raises(ValueError, match="check records"):
        prepare_checks(wrong_identity)

    (workspace.root / "tests").mkdir()
    unittest_plan = verify_plan(
        workspace,
        [{"unittest": {"discover": "tests", "pattern": "test_*.py"}}],
    )
    missing_path = replace(
        unittest_plan,
        checks=(replace(unittest_plan.checks[0], paths=()),),
    )
    with pytest.raises(ValueError, match="exactly one"):
        prepare_checks(missing_path)


def test_interruption_terminates_started_check_before_propagation(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = verify_plan(workspace, [{"import_check": {"modules": ["json"]}}])

    class InterruptedProcess:
        pid = 12345

        def wait(self, *, timeout=None):
            raise KeyboardInterrupt

        def poll(self):
            return 0

    monkeypatch.setattr(
        checks_module.subprocess,
        "Popen",
        lambda *args, **kwargs: InterruptedProcess(),
    )

    with pytest.raises(KeyboardInterrupt):
        run_checks(plan)


def test_process_termination_escalates_and_tolerates_signal_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operations: list[str] = []

    class StubbornProcess:
        pid = 12345

        def poll(self):
            return None

        def wait(self, *, timeout=None):
            raise subprocess.TimeoutExpired("check", timeout)

        def terminate(self):
            operations.append("terminate")
            raise OSError("terminate failed")

        def kill(self):
            operations.append("kill")

    monkeypatch.setattr(checks_module.os, "name", "posix")
    monkeypatch.setattr(process_module.signal, "SIGTERM", 15, raising=False)
    monkeypatch.setattr(process_module.signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(
        checks_module.os,
        "killpg",
        lambda *args: (_ for _ in ()).throw(OSError("signal failed")),
        raising=False,
    )

    checks_module._terminate_process(StubbornProcess())

    assert operations == ["terminate", "kill"]


def test_windows_signal_path_uses_direct_process_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operations: list[str] = []

    class Process:
        def terminate(self):
            operations.append("terminate")

        def kill(self):
            operations.append("kill")

    monkeypatch.setattr(checks_module.os, "name", "nt")

    checks_module._signal_process(Process(), force=False)
    checks_module._signal_process(Process(), force=True)

    assert operations == ["terminate", "kill"]


def test_process_group_options_are_platform_specific(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process_module.os, "name", "posix")
    assert process_module._process_group_options() == {"start_new_session": True}

    monkeypatch.setattr(process_module.os, "name", "nt")
    assert process_module._process_group_options() == {
        "creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    }
