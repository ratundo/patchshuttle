"""Source transfer validation and transactional rollback contracts."""

import hashlib

import pytest

from patchshuttle import Job, execute_plan
from patchshuttle._python_symbols import find_symbol_source
from patchshuttle._refactor import move_range, move_symbol
from patchshuttle.errors import ExecutionError, PlanningError, PolicyError
from patchshuttle.operations import rollback_job
from patchshuttle.planner import plan_job
from patchshuttle.workspace import init_workspace


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def symbol_hash(text, symbol="run"):
    return find_symbol_source(text, symbol, filename="a.py").ast_sha256


def transfer_job(ws, action, *, checks=None, prefix=()):
    return Job(
        protocol=1,
        project_id=ws.project_id,
        id="PATCH-MOVE",
        kind="patch",
        actions=[*prefix, action],
        checks=checks or [{"import_check": {"modules": ["json"]}}],
    )


def setup(tmp_path, source="def run():\n    return 7\n", target=""):
    ws = init_workspace(tmp_path).workspace
    (tmp_path / "a.py").write_text(source, encoding="utf-8")
    (tmp_path / "b.py").write_text(target, encoding="utf-8")
    return ws, {
        "move_symbol": {
            "path": "a.py",
            "symbol": "run",
            "expected_ast_sha256": symbol_hash(source),
            "destination": "b.py",
            "expected_destination_sha256": digest(target.encode()),
        }
    }


def test_move_range_preserves_lines_and_requires_safe_boundaries():
    assert move_range("a\nb\nc\n", "x\ny\n", start=2, end=2, line=2) == (
        "a\nc\n",
        "x\nb\ny\n",
    )
    assert move_range("a", "", start=1, end=1, line=1) == ("", "a")
    for dest, line, message in [
        ("x\n", 3, "destination_line"),
        ("x", 2, "preceding newline"),
        ("x\n", 1, "final newline"),
    ]:
        with pytest.raises(ValueError, match=message):
            move_range("a", dest, start=1, end=1, line=line)


def test_module_class_and_method_transfers():
    source = "# retained\ndef run():\n    return 1\n"
    a, b = move_symbol(source, "VALUE = 2", symbol="run", destination_class=None)
    assert a == "# retained\n" and b == "VALUE = 2\ndef run():\n    return 1\n"
    source = "class A:\n    def run(self):\n        return 1\n"
    a, b = move_symbol(
        source, "class B:\n    pass\n\nEND = 2\n", symbol="A.run", destination_class="B"
    )
    assert a == "class A:\n    pass\n"
    assert "    def run(self):\n        return 1\n\nEND" in b
    a, b = move_symbol(source, "", symbol="A.run", destination_class=None)
    assert b == "def run(self):\n    return 1\n"
    a, b = move_symbol("class A:\n    pass", "", symbol="A", destination_class=None)
    assert a == "" and b == "class A:\n    pass\n"
    a, b = move_symbol(
        "def run():\n    return 1\n",
        "class B:\n    pass\n",
        symbol="run",
        destination_class="B",
    )
    assert "    def run():" in b


@pytest.mark.parametrize(
    ("source", "symbol", "target", "scope", "message"),
    [
        ("def run():\n    pass\n", "missing", "", None, "resolve"),
        ("def run():\n    pass\ndef run():\n    pass\n", "run", "", None, "resolve"),
        (
            "def outer():\n    def run():\n        pass\n",
            "outer.run",
            "",
            None,
            "closure",
        ),
        (
            "def run():\n    pass\n",
            "run",
            "def target():\n    pass\n",
            "target",
            "class",
        ),
        (
            "def run():\n    pass\n",
            "run",
            "def run():\n    pass\n",
            None,
            "already contains",
        ),
        ("def run():\n    pass\n", "run", "class B: pass\n", "B", "one-line"),
        (
            "class A:\n    def run(self):\n        return super().run()\n",
            "A.run",
            "",
            None,
            "class-bound",
        ),
        (
            "class A:\n    def run(self):\n        return self.__value\n",
            "A.run",
            "",
            None,
            "class-bound",
        ),
        (
            "class A:\n    def run(self):\n        return __value\n",
            "A.run",
            "",
            None,
            "class-bound",
        ),
        (
            'class A:\n    def run(self):\n        return """a\nb"""\n',
            "A.run",
            "",
            None,
            "multiline",
        ),
    ],
)
def test_unsafe_symbol_moves_rejected(source, symbol, target, scope, message):
    with pytest.raises(ValueError, match=message):
        move_symbol(source, target, symbol=symbol, destination_class=scope)


def test_move_transaction_and_manual_rollback(tmp_path):
    ws, action = setup(tmp_path)
    original_a = (tmp_path / "a.py").read_bytes()
    original_b = (tmp_path / "b.py").read_bytes()
    plan = plan_job(transfer_job(ws, action), ws)
    assert len(plan.file_changes) == 2
    result = execute_plan(plan, approved=True)
    assert result.workspace_comparison.success
    assert (tmp_path / "a.py").read_bytes() == b""
    assert "return 7" in (tmp_path / "b.py").read_text()
    rollback_job(ws, "PATCH-MOVE", approved=True)
    assert (tmp_path / "a.py").read_bytes() == original_a
    assert (tmp_path / "b.py").read_bytes() == original_b


def test_failed_check_restores_both_files(tmp_path):
    ws, action = setup(tmp_path)
    original = (tmp_path / "a.py").read_bytes()
    value = transfer_job(
        ws,
        action,
        checks=[{"import_check": {"modules": ["module_that_does_not_exist"]}}],
    )
    with pytest.raises(ExecutionError):
        execute_plan(plan_job(value, ws), approved=True)
    assert (tmp_path / "a.py").read_bytes() == original
    assert (tmp_path / "b.py").read_bytes() == b""


@pytest.mark.parametrize(
    "changes",
    [
        {"destination": "a.py"},
        {"destination": "A.py"},
        {"expected_destination_sha256": "0" * 64},
        {"expected_ast_sha256": "0" * 64},
        {"symbol": "missing"},
        {"destination": "missing.py"},
        {"destination": "../outside.py"},
        {"destination": "patches/state/registry.json"},
    ],
)
def test_transfer_rejects_stale_or_invalid_paths_without_writes(tmp_path, changes):
    ws, action = setup(tmp_path)
    before = (tmp_path / "a.py").read_bytes()
    action["move_symbol"].update(changes)
    with pytest.raises((PlanningError, PolicyError)):
        plan_job(transfer_job(ws, action), ws)
    assert (tmp_path / "a.py").read_bytes() == before
    assert not (tmp_path / "b.py").read_bytes()


def test_range_move_to_new_file_preserves_crlf(tmp_path):
    ws = init_workspace(tmp_path).workspace
    (tmp_path / "a.txt").write_bytes(b"first\r\nsecond\r\n")
    action = {
        "move_range": {
            "path": "a.txt",
            "start_line": 2,
            "end_line": 2,
            "expected_content": "second\n",
            "destination": "b.txt",
            "destination_line": 1,
            "expected_destination_sha256": digest(b""),
        }
    }
    job = transfer_job(
        ws,
        action,
        prefix=[{"create_file": {"path": "b.txt", "content": "", "newline": "crlf"}}],
    )
    execute_plan(plan_job(job, ws), approved=True)
    assert (tmp_path / "a.txt").read_bytes() == b"first\r\n"
    assert (tmp_path / "b.txt").read_bytes() == b"second\r\n"


def test_range_move_cannot_leave_broken_python(tmp_path):
    ws, _ = setup(tmp_path)
    action = {
        "move_range": {
            "path": "a.py",
            "start_line": 2,
            "end_line": 2,
            "expected_content": "    return 7\n",
            "destination": "b.py",
            "destination_line": 1,
            "expected_destination_sha256": digest(b""),
        }
    }
    with pytest.raises(PlanningError):
        plan_job(transfer_job(ws, action), ws)


@pytest.mark.parametrize(
    "target", ["run = 1\n", "import os as run\n", "from os import run\n"]
)
def test_destination_bound_names_are_not_overwritten(target):
    with pytest.raises(ValueError, match="already binds"):
        move_symbol(
            "def run():\n    pass\n", target, symbol="run", destination_class=None
        )


def test_private_method_and_inconsistent_indent_rejected():
    from patchshuttle._refactor import _reindent

    with pytest.raises(ValueError, match="private declaration"):
        move_symbol(
            "class A:\n    def __run(self):\n        pass\n",
            "",
            symbol="A.__run",
            destination_class=None,
        )
    with pytest.raises(ValueError, match="inconsistent indentation"):
        _reindent("    def run():\n        return (\n1\n)\n", "    ", "")
    assert (
        _reindent("    def run():\n\n        pass\n", "    ", "")
        == "def run():\n\n    pass\n"
    )


def test_ast_verification_rejects_transfer_corruption(monkeypatch):
    import patchshuttle._refactor as module

    monkeypatch.setattr(
        module, "_reindent", lambda *args: "def run():\n    return 999\n"
    )
    with pytest.raises(ValueError, match="changed the declaration"):
        move_symbol(
            "def run():\n    return 1\n", "", symbol="run", destination_class=None
        )


def test_constructors_and_hash_normalization():
    from patchshuttle.actions import move_range as range_action
    from patchshuttle.actions import move_symbol as symbol_action
    from patchshuttle.actions import replace_symbol

    value = range_action(
        "a.txt",
        "b.txt",
        start_line=1,
        end_line=2,
        destination_line=1,
        expected_content="a\nb\n",
        expected_destination_sha256="A" * 64,
    )
    assert value.parameters.expected_destination_sha256 == "a" * 64
    value = symbol_action(
        "a.py",
        "run",
        "b.py",
        expected_ast_sha256="B" * 64,
        expected_destination_sha256="C" * 64,
    )
    assert value.parameters.expected_ast_sha256 == "b" * 64
    assert value.parameters.expected_destination_sha256 == "c" * 64
    assert (
        replace_symbol(
            "a.py", "run", "def run(): pass", expected_ast_sha256="D" * 64
        ).parameters.expected_ast_sha256
        == "d" * 64
    )


def test_non_python_symbol_transfer_rejected(tmp_path):
    ws, action = setup(tmp_path)
    (tmp_path / "b.txt").write_text("")
    action["move_symbol"]["destination"] = "b.txt"
    with pytest.raises(PlanningError, match="requires Python files"):
        plan_job(transfer_job(ws, action), ws)


def test_stale_plan_does_not_move_anything(tmp_path):
    ws, action = setup(tmp_path)
    plan = plan_job(transfer_job(ws, action), ws)
    (tmp_path / "b.py").write_text("EXTERNAL = 1\n")
    with pytest.raises(ExecutionError):
        execute_plan(plan, approved=True)
    assert "return 7" in (tmp_path / "a.py").read_text()
    assert (tmp_path / "b.py").read_text() == "EXTERNAL = 1\n"
