"""Refresh only recognized generated prefixes and retain owner text and recovery bytes."""

import hashlib
from pathlib import Path

import pytest
from click.testing import CliRunner

from patchshuttle.cli import main
from patchshuttle.documentation import refresh as m
from patchshuttle.workspace import init_workspace


@pytest.fixture
def ws(tmp_path):
    return init_workspace(tmp_path).workspace


def old_guide(ws, newline="\n", bom=b""):
    text = (Path(__file__).parent / "fixtures/a6-guide.md").read_text(encoding="utf-8")
    text = text.replace("{{PROJECT_ID}}", ws.project_id).replace("\n", newline)
    data = (
        bom
        + text.encode()
        + "\n## Owner notes\nКирилиця: keep these instructions.\n".encode()
    )
    (ws.patches_dir / "AI_GUIDE.md").write_bytes(data)
    return data


@pytest.mark.parametrize("newline,bom", [("\n", b""), ("\r\n", b"\xef\xbb\xbf")])
def test_review_apply_and_repeat_preserve_notes_and_backup(ws, newline, bom):
    before = old_guide(ws, newline, bom)
    plan = m.plan_guide_refresh(ws)
    assert plan.status == "UPDATE_AVAILABLE"
    assert (ws.patches_dir / "AI_GUIDE.md").read_bytes() == before
    assert m.apply_guide_refresh(ws, plan.expected_plan) == "UPDATED"
    after = (ws.patches_dir / "AI_GUIDE.md").read_bytes()
    assert after == plan.replacement
    assert after.endswith(
        "\n## Owner notes\nКирилиця: keep these instructions.\n".encode()
    )
    assert after.startswith(bom)
    backup = (
        ws.patches_dir
        / "state"
        / ("ai-guide-" + hashlib.sha256(before).hexdigest() + ".bak")
    )
    assert backup.read_bytes() == before
    current = m.plan_guide_refresh(ws)
    assert current.status == "CURRENT"
    assert m.apply_guide_refresh(ws, current.expected_plan) == "CURRENT"
    # Existing exact backup is reused after an owner restores the old guide.
    (ws.patches_dir / "AI_GUIDE.md").write_bytes(before)
    assert m.apply_guide_refresh(ws, plan.expected_plan) == "UPDATED"


def test_stale_guard_and_manual_edits_are_not_overwritten(ws):
    old_guide(ws)
    plan = m.plan_guide_refresh(ws)
    path = ws.patches_dir / "AI_GUIDE.md"
    path.write_bytes(path.read_bytes() + b"new owner note\n")
    with pytest.raises(ValueError, match="GUIDE_PLAN_MISMATCH"):
        m.apply_guide_refresh(ws, plan.expected_plan)
    path.write_bytes(b"custom instructions\n")
    with pytest.raises(ValueError, match="MANUAL_REVIEW_REQUIRED"):
        m.plan_guide_refresh(ws)
    assert path.read_bytes() == b"custom instructions\n"


def test_size_and_unsafe_paths(ws, monkeypatch):
    path = ws.patches_dir / "AI_GUIDE.md"
    monkeypatch.setattr(m, "MAX_GUIDE_BYTES", 1)
    with pytest.raises(ValueError, match="size limit"):
        m.plan_guide_refresh(ws)
    path.unlink()
    path.mkdir()
    with pytest.raises(ValueError, match="regular file"):
        m.plan_guide_refresh(ws)
    state = ws.patches_dir / "state"
    # Test the directory validator directly without altering workspace state.
    fake = type("Workspace", (), {"patches_dir": state / "run.lock"})()
    with pytest.raises(ValueError, match="real directory"):
        m._directories(fake)


def test_failed_replace_retains_original_and_backup(ws, monkeypatch):
    before = old_guide(ws)
    plan = m.plan_guide_refresh(ws)

    def fail(*args):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(m.os, "replace", fail)
    with pytest.raises(Exception, match="workspace lock could not"):
        m.apply_guide_refresh(ws, plan.expected_plan)
    assert (ws.patches_dir / "AI_GUIDE.md").read_bytes() == before
    assert not list(ws.patches_dir.glob(".ai-guide-*.tmp"))
    assert (
        next((ws.patches_dir / "state").glob("ai-guide-*.bak")).read_bytes() == before
    )


def test_corrupt_backup_refused(ws):
    before = old_guide(ws)
    plan = m.plan_guide_refresh(ws)
    backup = (
        ws.patches_dir
        / "state"
        / ("ai-guide-" + hashlib.sha256(before).hexdigest() + ".bak")
    )
    backup.write_bytes(b"not the original")
    with pytest.raises(ValueError, match="backup does not match"):
        m.apply_guide_refresh(ws, plan.expected_plan)


def test_change_during_write_is_not_replaced(ws, monkeypatch):
    old_guide(ws)
    plan = m.plan_guide_refresh(ws)
    original = m.plan_guide_refresh
    calls = 0

    def changed(workspace):
        nonlocal calls
        calls += 1
        if calls == 2:
            path = ws.patches_dir / "AI_GUIDE.md"
            path.write_bytes(path.read_bytes() + b"late note\n")
        return original(workspace)

    monkeypatch.setattr(m, "plan_guide_refresh", changed)
    with pytest.raises(ValueError, match="changed during refresh"):
        m.apply_guide_refresh(ws, plan.expected_plan)
    assert (ws.patches_dir / "AI_GUIDE.md").read_bytes().endswith(b"late note\n")


def test_temp_collision_does_not_delete_existing_file(ws, monkeypatch):
    old_guide(ws)
    plan = m.plan_guide_refresh(ws)
    monkeypatch.setattr(m.uuid, "uuid4", lambda: type("ID", (), {"hex": "collision"})())
    temporary = ws.patches_dir / ".ai-guide-collision.tmp"
    temporary.write_bytes(b"keep")
    with pytest.raises(Exception, match="workspace lock could not"):
        m.apply_guide_refresh(ws, plan.expected_plan)
    assert temporary.read_bytes() == b"keep"


def test_cli_preview_apply_errors_and_discovery(ws, monkeypatch):
    runner = CliRunner()
    prefix = ["--workspace", str(ws.root), "docs", "refresh"]
    old_guide(ws)
    plan = m.plan_guide_refresh(ws)
    result = runner.invoke(main, prefix)
    assert result.exit_code == 0, result.output
    assert "UPDATE_AVAILABLE" in result.output
    assert "--- AI_GUIDE.md" in result.output
    result = runner.invoke(
        main, prefix + ["--apply", "--expected-plan", plan.expected_plan]
    )
    assert result.exit_code == 0, result.output
    assert "UPDATED" in result.output
    for arguments in (["--apply"], ["--expected-plan", "bad"]):
        assert runner.invoke(main, prefix + arguments).exit_code == 2
    assert (
        runner.invoke(main, prefix + ["--apply", "--expected-plan", "bad"]).exit_code
        == 1
    )
    monkeypatch.chdir(ws.root)
    assert "CURRENT" in runner.invoke(main, ["docs", "refresh"]).output


def test_cli_bounds_large_diff(ws, monkeypatch):
    from patchshuttle.documentation import cli

    plan = m.GuidePlan(b"old\n", b"x" * 50000 + b"\n", "digest", "UPDATE_AVAILABLE")
    monkeypatch.setattr(cli, "plan_guide_refresh", lambda workspace: plan)
    result = CliRunner().invoke(main, ["--workspace", str(ws.root), "docs", "refresh"])
    assert result.exit_code == 0
    assert "PREVIEW_TRUNCATED" in result.output
    assert len(result.output) < 40500
