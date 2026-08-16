"""Contract tests for immutable, read-only job planning."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path, PurePosixPath

import pytest

import patchshuttle.planner as planner_module
import patchshuttle.workspace as workspace_module
from patchshuttle import Job
from patchshuttle.errors import PlanningError, PlanningErrorCode, PolicyError
from patchshuttle.planner import (
    ActionDisposition,
    FileDisposition,
    NewlineStyle,
    Plan,
    plan_job,
)
from patchshuttle.policy import PathKind, Policy, WorkspacePath
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


def make_job(
    *,
    job_id: str = "PATCH-001",
    kind: str = "patch",
    actions: list[dict] | None = None,
    checks: list[dict] | None = None,
) -> Job:
    return Job(
        protocol=1,
        project_id=PROJECT_ID,
        id=job_id,
        kind=kind,
        actions=actions or [],
        checks=checks or [],
    )


def snapshot(root: Path) -> dict[Path, bytes]:
    return {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def test_audit_plan_is_immutable_complete_and_read_only(workspace: Workspace) -> None:
    source = workspace.root / "src"
    source.mkdir()
    readme = workspace.root / "README.md"
    readme.write_text("# Example\n", encoding="utf-8")
    job = make_job(
        job_id="AUDIT-001",
        kind="audit",
        actions=[
            {"tree": {"path": "."}},
            {"read": {"path": "README.md"}},
            {"search": {"path": "src", "text": "TODO"}},
            {"find_files": {"path": "src", "glob": "*.py"}},
            {"file_info": {"path": "README.md"}},
            {"hash": {"path": "README.md"}},
            {"git_status": {}},
            {"environment": {}},
        ],
    )
    before = snapshot(workspace.root)

    plan = plan_job(job, workspace)

    assert isinstance(plan, Plan)
    assert plan.workspace == workspace
    assert plan.job == job
    assert len(plan.job_hash) == 64
    assert [action.id for action in plan.actions] == [
        f"action_{index:03d}" for index in range(1, 9)
    ]
    assert {action.disposition for action in plan.actions} == {
        ActionDisposition.INSPECT
    }
    assert plan.files_to_create == ()
    assert plan.files_to_modify == ()
    assert plan.directories_to_create == ()
    assert plan.checks == ()
    assert plan.formatting_targets == ()
    assert plan.backup_destination is None
    assert plan.auto_rollback is False
    assert plan.requires_confirmation is False
    assert plan.protected_paths_passed is True
    assert {item.path for item in plan.fingerprints} == {
        PurePosixPath("."),
        PurePosixPath("README.md"),
        PurePosixPath("src"),
    }
    assert snapshot(workspace.root) == before

    with pytest.raises(FrozenInstanceError):
        plan.job_hash = "changed"  # type: ignore[misc]


def test_semantically_equal_jobs_have_the_same_normalized_hash(
    workspace: Workspace,
) -> None:
    implicit = make_job(
        job_id="AUDIT-001",
        kind="audit",
        actions=[{"tree": {}}],
    )
    explicit = make_job(
        job_id="AUDIT-001",
        kind="audit",
        actions=[
            {
                "tree": {
                    "path": ".",
                    "depth": 4,
                    "max_entries": 500,
                    "include_hidden": False,
                }
            }
        ],
    )
    changed = make_job(
        job_id="AUDIT-001",
        kind="audit",
        actions=[{"tree": {"depth": 5}}],
    )

    assert (
        plan_job(implicit, workspace).job_hash == plan_job(explicit, workspace).job_hash
    )
    assert (
        plan_job(implicit, workspace).job_hash != plan_job(changed, workspace).job_hash
    )


def test_plan_job_accepts_a_descendant_workspace_path(workspace: Workspace) -> None:
    child = workspace.root / "src/package"
    child.mkdir(parents=True)
    job = make_job(
        job_id="AUDIT-001",
        kind="audit",
        actions=[{"tree": {}}],
    )

    plan = plan_job(job, child)

    assert plan.workspace == workspace


@pytest.mark.parametrize(
    ("action", "expected_code"),
    (
        ({"tree": {"path": "README.md"}}, PlanningErrorCode.TARGET_TYPE_INVALID),
        ({"read": {"path": "src"}}, PlanningErrorCode.TARGET_TYPE_INVALID),
        ({"hash": {"path": "src"}}, PlanningErrorCode.TARGET_TYPE_INVALID),
        (
            {"find_files": {"path": "README.md", "glob": "*.py"}},
            PlanningErrorCode.TARGET_TYPE_INVALID,
        ),
    ),
)
def test_audit_target_type_is_planned(
    workspace: Workspace,
    action: dict,
    expected_code: PlanningErrorCode,
) -> None:
    (workspace.root / "src").mkdir()
    (workspace.root / "README.md").write_text("project\n", encoding="utf-8")
    job = make_job(job_id="AUDIT-001", kind="audit", actions=[action])

    with pytest.raises(PlanningError) as caught:
        plan_job(job, workspace)

    assert caught.value.code is expected_code
    assert caught.value.item_id == "action_001"


def test_audit_rejects_protected_and_ignored_targets(workspace: Workspace) -> None:
    (workspace.root / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    cache = workspace.root / ".pytest_cache"
    cache.mkdir()

    protected_job = make_job(
        job_id="AUDIT-001",
        kind="audit",
        actions=[{"read": {"path": ".env"}}],
    )
    ignored_job = make_job(
        job_id="AUDIT-002",
        kind="audit",
        actions=[{"tree": {"path": ".pytest_cache"}}],
    )

    with pytest.raises(PolicyError):
        plan_job(protected_job, workspace)
    with pytest.raises(PlanningError) as caught:
        plan_job(ignored_job, workspace)

    assert caught.value.code is PlanningErrorCode.PATH_IGNORED


def test_audit_read_enforces_binary_and_size_limits(workspace: Workspace) -> None:
    binary = workspace.root / "binary.dat"
    binary.write_bytes(b"text\0binary")
    binary_job = make_job(
        job_id="AUDIT-001",
        kind="audit",
        actions=[{"read": {"path": "binary.dat"}}],
    )

    with pytest.raises(PlanningError) as binary_error:
        plan_job(binary_job, workspace)
    assert binary_error.value.code is PlanningErrorCode.FILE_BINARY

    readme = workspace.root / "README.md"
    readme.write_text("project\n", encoding="utf-8")
    limited = make_job(
        job_id="AUDIT-002",
        kind="audit",
        actions=[{"read": {"path": "README.md", "max_bytes": 1_000_001}}],
    )
    with pytest.raises(PlanningError) as limit_error:
        plan_job(limited, workspace)
    assert limit_error.value.code is PlanningErrorCode.FILE_SIZE_LIMIT_EXCEEDED


def test_audit_search_of_one_file_validates_its_text(workspace: Workspace) -> None:
    target = workspace.root / "notes.txt"
    target.write_text("TODO\n", encoding="utf-8")
    job = make_job(
        job_id="AUDIT-001",
        kind="audit",
        actions=[{"search": {"path": "notes.txt", "text": "TODO"}}],
    )

    plan = plan_job(job, workspace)

    assert plan.actions[0].paths == (PurePosixPath("notes.txt"),)
    assert plan.fingerprints[0].sha256 is not None


def test_patch_plan_requires_checks_under_default_local_policy(
    workspace: Workspace,
) -> None:
    job = make_job(actions=[{"create_directory": {"path": "src"}}])

    with pytest.raises(PlanningError) as caught:
        plan_job(job, workspace)

    assert caught.value.code is PlanningErrorCode.PATCH_CHECK_REQUIRED


def test_local_policy_can_allow_patch_without_checks(workspace: Workspace) -> None:
    checks = workspace.config.checks.model_copy(
        update={"require_at_least_one_for_patch": False}
    )
    config = workspace.config.model_copy(update={"checks": checks})
    configured = Workspace(root=workspace.root, config=config)
    job = make_job(actions=[{"create_directory": {"path": "src"}}])

    plan = plan_job(job, configured)

    assert plan.directories_to_create == (PurePosixPath("src"),)


def test_action_count_limit_is_enforced(workspace: Workspace) -> None:
    execution = workspace.config.execution.model_copy(update={"max_actions": 1})
    config = workspace.config.model_copy(update={"execution": execution})
    configured = Workspace(root=workspace.root, config=config)
    job = make_job(
        job_id="AUDIT-001",
        kind="audit",
        actions=[{"tree": {}}, {"environment": {}}],
    )

    with pytest.raises(PlanningError) as caught:
        plan_job(job, configured)

    assert caught.value.code is PlanningErrorCode.ACTION_LIMIT_EXCEEDED


def test_create_directory_plans_missing_parents_and_existing_no_change(
    workspace: Workspace,
) -> None:
    (workspace.root / "existing").mkdir()
    job = make_job(
        actions=[
            {"create_directory": {"path": "src/example/package"}},
            {"create_directory": {"path": "existing"}},
        ],
        checks=[{"compileall": {"paths": ["src"]}}],
    )

    plan = plan_job(job, workspace)

    assert plan.directories_to_create == (
        PurePosixPath("src"),
        PurePosixPath("src/example"),
        PurePosixPath("src/example/package"),
    )
    assert [action.disposition for action in plan.actions] == [
        ActionDisposition.CREATE,
        ActionDisposition.NO_CHANGE,
    ]
    assert plan.file_changes == ()


def test_create_directory_rejects_existing_file(workspace: Workspace) -> None:
    (workspace.root / "src").write_text("not a directory\n", encoding="utf-8")
    job = make_job(
        actions=[{"create_directory": {"path": "src"}}],
        checks=[{"import_check": {"modules": ["patchshuttle"]}}],
    )

    with pytest.raises(PlanningError) as caught:
        plan_job(job, workspace)

    assert caught.value.code is PlanningErrorCode.TARGET_TYPE_INVALID


def test_repeated_virtual_directory_is_no_change(workspace: Workspace) -> None:
    job = make_job(
        actions=[
            {"create_directory": {"path": "src"}},
            {"create_directory": {"path": "src"}},
        ],
        checks=[{"compileall": {"paths": ["src"]}}],
    )

    plan = plan_job(job, workspace)

    assert [action.disposition for action in plan.actions] == [
        ActionDisposition.CREATE,
        ActionDisposition.NO_CHANGE,
    ]


def test_create_file_plans_bytes_parents_hashes_and_formatting(
    workspace: Workspace,
) -> None:
    job = make_job(
        actions=[
            {
                "create_file": {
                    "path": "src/example/module.py",
                    "content": "VALUE = 1\n",
                    "newline": "crlf",
                }
            }
        ],
        checks=[{"compileall": {"paths": ["src"]}}],
    )
    before = snapshot(workspace.root)

    plan = plan_job(job, workspace)

    assert plan.directories_to_create == (
        PurePosixPath("src"),
        PurePosixPath("src/example"),
    )
    assert plan.files_to_create == (PurePosixPath("src/example/module.py"),)
    assert plan.files_to_modify == ()
    assert plan.formatting_targets == (PurePosixPath("src/example/module.py"),)
    assert plan.requires_confirmation is True
    assert plan.backup_destination == PurePosixPath(
        "patches/backups/PATCH-001/<RUN_TIMESTAMP>"
    )
    assert plan.auto_rollback is True
    change = plan.file_changes[0]
    assert change.disposition is FileDisposition.CREATE
    assert change.before_sha256 is None
    assert change.after_sha256 is not None
    assert change.before_size is None
    assert change.after_size == len(b"VALUE = 1\r\n")
    assert change.encoding == "utf-8"
    assert change.newline is NewlineStyle.CRLF
    assert change.content == b"VALUE = 1\r\n"
    assert snapshot(workspace.root) == before


def test_create_file_reuses_virtual_and_existing_parent_directories(
    workspace: Workspace,
) -> None:
    (workspace.root / "existing").mkdir()
    job = make_job(
        actions=[
            {"create_directory": {"path": "src"}},
            {"create_file": {"path": "src/one.py", "content": "pass\n"}},
            {
                "create_file": {
                    "path": "existing/two.py",
                    "content": "pass\n",
                }
            },
        ],
        checks=[{"compileall": {"paths": ["src", "existing"]}}],
    )

    plan = plan_job(job, workspace)

    assert plan.directories_to_create == (PurePosixPath("src"),)
    assert plan.files_to_create == (
        PurePosixPath("src/one.py"),
        PurePosixPath("existing/two.py"),
    )
    assert PurePosixPath("existing") in {item.path for item in plan.fingerprints}


def test_create_file_existing_identical_is_no_change(workspace: Workspace) -> None:
    target = workspace.root / "example.txt"
    target.write_bytes(b"same\n")
    job = make_job(
        actions=[{"create_file": {"path": "example.txt", "content": "same\n"}}],
        checks=[{"import_check": {"modules": ["patchshuttle"]}}],
    )

    plan = plan_job(job, workspace)

    assert plan.actions[0].disposition is ActionDisposition.NO_CHANGE
    assert plan.file_changes == ()


def test_create_file_rejects_conflict_directory_bad_encoding_and_size(
    workspace: Workspace,
) -> None:
    (workspace.root / "conflict.txt").write_text("old\n", encoding="utf-8")
    (workspace.root / "directory").mkdir()
    base_check = [{"import_check": {"modules": ["patchshuttle"]}}]
    jobs = (
        (
            make_job(
                actions=[{"create_file": {"path": "conflict.txt", "content": "new\n"}}],
                checks=base_check,
            ),
            PlanningErrorCode.CREATE_FILE_CONFLICT,
        ),
        (
            make_job(
                actions=[{"create_file": {"path": "directory", "content": "x"}}],
                checks=base_check,
            ),
            PlanningErrorCode.TARGET_TYPE_INVALID,
        ),
        (
            make_job(
                actions=[
                    {
                        "create_file": {
                            "path": "bad.txt",
                            "content": "x",
                            "encoding": "missing-codec",
                        }
                    }
                ],
                checks=base_check,
            ),
            PlanningErrorCode.CONTENT_ENCODING_INVALID,
        ),
    )
    for job, code in jobs:
        with pytest.raises(PlanningError) as caught:
            plan_job(job, workspace)
        assert caught.value.code is code

    execution = workspace.config.execution.model_copy(
        update={"max_single_file_bytes": 1}
    )
    configured = Workspace(
        root=workspace.root,
        config=workspace.config.model_copy(update={"execution": execution}),
    )
    oversized = make_job(
        actions=[{"create_file": {"path": "large.txt", "content": "xx"}}],
        checks=base_check,
    )
    with pytest.raises(PlanningError) as caught:
        plan_job(oversized, configured)
    assert caught.value.code is PlanningErrorCode.FILE_SIZE_LIMIT_EXCEEDED


def test_create_file_rejects_binary_control_content(workspace: Workspace) -> None:
    job = make_job(
        actions=[{"create_file": {"path": "binary.txt", "content": "text\0data"}}],
        checks=[{"import_check": {"modules": ["patchshuttle"]}}],
    )

    with pytest.raises(PlanningError) as caught:
        plan_job(job, workspace)

    assert caught.value.code is PlanningErrorCode.CONTENT_BINARY_FORBIDDEN


def test_replace_exact_preserves_utf8_bom_and_crlf(workspace: Workspace) -> None:
    target = workspace.root / "module.py"
    target.write_bytes(b"\xef\xbb\xbfVALUE = 1\r\nVALUE = 1\r\n")
    job = make_job(
        actions=[
            {
                "replace_exact": {
                    "path": "module.py",
                    "old": "VALUE = 1\n",
                    "new": "VALUE = 2\n",
                    "expected_count": 2,
                }
            }
        ],
        checks=[{"compileall": {"paths": ["module.py"]}}],
    )

    plan = plan_job(job, workspace)

    change = plan.file_changes[0]
    assert change.disposition is FileDisposition.MODIFY
    assert change.encoding == "utf-8-sig"
    assert change.newline is NewlineStyle.CRLF
    assert change.content == b"\xef\xbb\xbfVALUE = 2\r\nVALUE = 2\r\n"
    assert plan.files_to_modify == (PurePosixPath("module.py"),)


def test_replace_exact_no_change_and_count_mismatch(workspace: Workspace) -> None:
    target = workspace.root / "settings.py"
    target.write_text("MODE = 'new'\n", encoding="utf-8")
    no_change = make_job(
        actions=[
            {
                "replace_exact": {
                    "path": "settings.py",
                    "old": "MODE = 'old'",
                    "new": "MODE = 'new'",
                }
            }
        ],
        checks=[{"compileall": {"paths": ["settings.py"]}}],
    )
    mismatch = make_job(
        actions=[
            {
                "replace_exact": {
                    "path": "settings.py",
                    "old": "MODE",
                    "new": "STATE",
                    "expected_count": 2,
                }
            }
        ],
        checks=[{"compileall": {"paths": ["settings.py"]}}],
    )

    assert plan_job(no_change, workspace).actions[0].disposition is (
        ActionDisposition.NO_CHANGE
    )
    with pytest.raises(PlanningError) as caught:
        plan_job(mismatch, workspace)
    assert caught.value.code is PlanningErrorCode.OCCURRENCE_COUNT_MISMATCH


@pytest.mark.parametrize(
    ("bom", "codec", "encoding"),
    (
        (b"\xff\xfe\x00\x00", "utf-32-le", "utf-32-le"),
        (b"\x00\x00\xfe\xff", "utf-32-be", "utf-32-be"),
        (b"\xff\xfe", "utf-16-le", "utf-16-le"),
        (b"\xfe\xff", "utf-16-be", "utf-16-be"),
    ),
)
def test_bom_text_encodings_are_preserved(
    workspace: Workspace,
    bom: bytes,
    codec: str,
    encoding: str,
) -> None:
    target = workspace.root / "text.txt"
    target.write_bytes(bom + "old\n".encode(codec))
    job = make_job(
        actions=[
            {
                "replace_exact": {
                    "path": "text.txt",
                    "old": "old",
                    "new": "new",
                }
            }
        ],
        checks=[{"import_check": {"modules": ["patchshuttle"]}}],
    )

    plan = plan_job(job, workspace)

    assert plan.file_changes[0].encoding == encoding
    assert plan.file_changes[0].content == bom + "new\n".encode(codec)


@pytest.mark.parametrize(
    ("raw", "code"),
    (
        (b"\xffinvalid", PlanningErrorCode.FILE_ENCODING_UNSUPPORTED),
        (b"text\x01control", PlanningErrorCode.FILE_BINARY),
    ),
)
def test_unsupported_encoding_and_binary_controls_are_rejected(
    workspace: Workspace,
    raw: bytes,
    code: PlanningErrorCode,
) -> None:
    (workspace.root / "target.txt").write_bytes(raw)
    job = make_job(
        actions=[
            {
                "replace_exact": {
                    "path": "target.txt",
                    "old": "text",
                    "new": "value",
                }
            }
        ],
        checks=[{"import_check": {"modules": ["patchshuttle"]}}],
    )

    with pytest.raises(PlanningError) as caught:
        plan_job(job, workspace)

    assert caught.value.code is code


def test_adding_first_newline_records_lf_style(workspace: Workspace) -> None:
    (workspace.root / "target.txt").write_bytes(b"old")
    job = make_job(
        actions=[
            {
                "replace_exact": {
                    "path": "target.txt",
                    "old": "old",
                    "new": "new\n",
                }
            }
        ],
        checks=[{"import_check": {"modules": ["patchshuttle"]}}],
    )

    plan = plan_job(job, workspace)

    assert plan.file_changes[0].newline is NewlineStyle.LF
    assert plan.file_changes[0].content == b"new\n"


def test_unrepresentable_planned_text_has_a_stable_error(
    workspace: Workspace,
) -> None:
    (workspace.root / "target.txt").write_text("old", encoding="utf-8")
    job = make_job(
        actions=[
            {
                "replace_exact": {
                    "path": "target.txt",
                    "old": "old",
                    "new": "\ud800",
                }
            }
        ],
        checks=[{"import_check": {"modules": ["patchshuttle"]}}],
    )

    with pytest.raises(PlanningError) as caught:
        plan_job(job, workspace)

    assert caught.value.code is PlanningErrorCode.CONTENT_ENCODING_INVALID


@pytest.mark.parametrize(
    ("name", "parameters", "expected"),
    (
        (
            "insert_before",
            {"anchor": "VALUE = 1", "content": "# generated\n"},
            "# generated\nVALUE = 1\n",
        ),
        (
            "insert_after",
            {"anchor": "VALUE = 1", "content": "\n# generated"},
            "VALUE = 1\n# generated\n",
        ),
        (
            "delete_exact",
            {"text": "VALUE = 1\n"},
            "",
        ),
    ),
)
def test_exact_edit_actions_are_dry_run_in_memory(
    workspace: Workspace,
    name: str,
    parameters: dict,
    expected: str,
) -> None:
    target = workspace.root / "module.py"
    target.write_bytes(b"VALUE = 1\n")
    job = make_job(
        actions=[{name: {"path": "module.py", **parameters}}],
        checks=[{"compileall": {"paths": ["module.py"]}}],
    )

    plan = plan_job(job, workspace)

    assert plan.file_changes[0].content == expected.encode()
    assert target.read_text(encoding="utf-8") == "VALUE = 1\n"


@pytest.mark.parametrize("name", ("insert_before", "insert_after"))
def test_insert_actions_detect_complete_and_partial_idempotent_state(
    workspace: Workspace,
    name: str,
) -> None:
    target = workspace.root / "module.py"
    content = "# generated\n" if name == "insert_before" else "\n# generated"
    target.write_text(
        (
            "# generated\nVALUE = 1\n"
            if name == "insert_before"
            else "VALUE = 1\n# generated\n"
        ),
        encoding="utf-8",
    )
    job = make_job(
        actions=[
            {
                name: {
                    "path": "module.py",
                    "anchor": "VALUE = 1",
                    "content": content,
                }
            }
        ],
        checks=[{"compileall": {"paths": ["module.py"]}}],
    )

    assert plan_job(job, workspace).actions[0].disposition is (
        ActionDisposition.NO_CHANGE
    )

    target.write_text(
        (
            "# generated\nVALUE = 1\nVALUE = 1\n"
            if name == "insert_before"
            else "VALUE = 1\n# generated\nVALUE = 1\n"
        ),
        encoding="utf-8",
    )
    partial = job.model_copy(
        update={
            "actions": (
                job.actions[0].model_copy(
                    update={
                        "root": job.actions[0].root.model_copy(
                            update={
                                name: job.actions[0].parameters.model_copy(
                                    update={"expected_count": 2}
                                )
                            }
                        )
                    }
                ),
            )
        }
    )
    with pytest.raises(PlanningError) as caught:
        plan_job(partial, workspace)
    assert caught.value.code is PlanningErrorCode.INSERTION_STATE_CONFLICT


def test_insert_and_delete_count_mismatches_are_rejected(
    workspace: Workspace,
) -> None:
    (workspace.root / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    cases = (
        {
            "insert_before": {
                "path": "module.py",
                "anchor": "VALUE = 1",
                "content": "# generated\n",
                "expected_count": 2,
            }
        },
        {
            "delete_exact": {
                "path": "module.py",
                "text": "missing",
            }
        },
    )
    for action in cases:
        job = make_job(
            actions=[action],
            checks=[{"compileall": {"paths": ["module.py"]}}],
        )
        with pytest.raises(PlanningError) as caught:
            plan_job(job, workspace)
        assert caught.value.code is PlanningErrorCode.OCCURRENCE_COUNT_MISMATCH


def test_sequential_actions_share_one_virtual_file(workspace: Workspace) -> None:
    job = make_job(
        actions=[
            {"create_file": {"path": "module.py", "content": "VALUE = 1\n"}},
            {
                "replace_exact": {
                    "path": "module.py",
                    "old": "VALUE = 1",
                    "new": "VALUE = 2",
                }
            },
            {
                "insert_after": {
                    "path": "module.py",
                    "anchor": "VALUE = 2",
                    "content": "\nREADY = True",
                }
            },
        ],
        checks=[{"compileall": {"paths": ["module.py"]}}],
    )

    plan = plan_job(job, workspace)

    assert plan.file_changes[0].content == b"VALUE = 2\nREADY = True\n"
    assert plan.files_to_create == (PurePosixPath("module.py"),)
    assert not (workspace.root / "module.py").exists()


def test_apply_diff_rejects_a_file_created_earlier_in_the_job(
    workspace: Workspace,
) -> None:
    diff = """\
--- a/module.py
+++ b/module.py
@@ -1 +1 @@
-VALUE = 1
+VALUE = 2
"""
    job = make_job(
        actions=[
            {"create_file": {"path": "module.py", "content": "VALUE = 1\n"}},
            {"apply_diff": {"diff": diff}},
        ],
        checks=[{"compileall": {"paths": ["module.py"]}}],
    )

    with pytest.raises(PlanningError) as caught:
        plan_job(job, workspace)

    assert caught.value.code is PlanningErrorCode.DIFF_PATH_INVALID


def test_virtual_file_cannot_be_used_as_a_parent(workspace: Workspace) -> None:
    job = make_job(
        actions=[
            {"create_file": {"path": "src", "content": "file"}},
            {"create_file": {"path": "src/module.py", "content": "pass\n"}},
        ],
        checks=[{"import_check": {"modules": ["patchshuttle"]}}],
    )

    with pytest.raises(PlanningError) as caught:
        plan_job(job, workspace)

    assert caught.value.code is PlanningErrorCode.VIRTUAL_PATH_CONFLICT


def test_text_action_rejects_virtual_file_parent_planned_directory_and_missing_file(
    workspace: Workspace,
) -> None:
    checks = [{"import_check": {"modules": ["patchshuttle"]}}]
    jobs = (
        (
            make_job(
                actions=[
                    {"create_file": {"path": "src", "content": "file"}},
                    {
                        "replace_exact": {
                            "path": "src/module.py",
                            "old": "a",
                            "new": "b",
                        }
                    },
                ],
                checks=checks,
            ),
            PlanningErrorCode.VIRTUAL_PATH_CONFLICT,
        ),
        (
            make_job(
                actions=[
                    {"create_directory": {"path": "src"}},
                    {
                        "replace_exact": {
                            "path": "src",
                            "old": "a",
                            "new": "b",
                        }
                    },
                ],
                checks=checks,
            ),
            PlanningErrorCode.TARGET_TYPE_INVALID,
        ),
        (
            make_job(
                actions=[
                    {
                        "replace_exact": {
                            "path": "missing.txt",
                            "old": "a",
                            "new": "b",
                        }
                    }
                ],
                checks=checks,
            ),
            PlanningErrorCode.TARGET_TYPE_INVALID,
        ),
    )
    for job, code in jobs:
        with pytest.raises(PlanningError) as caught:
            plan_job(job, workspace)
        assert caught.value.code is code


def test_binary_oversized_and_mixed_newline_modification_targets_are_rejected(
    workspace: Workspace,
) -> None:
    base_check = [{"import_check": {"modules": ["patchshuttle"]}}]
    cases = (
        ("binary.py", b"text\0binary", PlanningErrorCode.FILE_BINARY),
        (
            "mixed.py",
            b"one\r\ntwo\n",
            PlanningErrorCode.FILE_NEWLINE_UNSUPPORTED,
        ),
    )
    for name, raw, expected in cases:
        (workspace.root / name).write_bytes(raw)
        job = make_job(
            actions=[
                {
                    "replace_exact": {
                        "path": name,
                        "old": "text",
                        "new": "value",
                    }
                }
            ],
            checks=base_check,
        )
        with pytest.raises(PlanningError) as caught:
            plan_job(job, workspace)
        assert caught.value.code is expected

    execution = workspace.config.execution.model_copy(
        update={"max_single_file_bytes": 3}
    )
    configured = Workspace(
        root=workspace.root,
        config=workspace.config.model_copy(update={"execution": execution}),
    )
    (workspace.root / "large.py").write_bytes(b"pass\n")
    oversized = make_job(
        actions=[
            {
                "replace_exact": {
                    "path": "large.py",
                    "old": "pass",
                    "new": "done",
                }
            }
        ],
        checks=base_check,
    )
    with pytest.raises(PlanningError) as caught:
        plan_job(oversized, configured)
    assert caught.value.code is PlanningErrorCode.FILE_SIZE_LIMIT_EXCEEDED


def test_valid_unified_diff_is_dry_run_for_multiple_files(workspace: Workspace) -> None:
    (workspace.root / "one.txt").write_bytes(b"old one\n")
    (workspace.root / "two.txt").write_bytes(b"old two\n")
    diff = """\
--- a/one.txt
+++ b/one.txt
@@ -1 +1 @@
-old one
+new one
--- a/two.txt
+++ b/two.txt
@@ -1 +1 @@
-old two
+new two
"""
    job = make_job(
        actions=[{"apply_diff": {"diff": diff, "strip": 1}}],
        checks=[{"import_check": {"modules": ["patchshuttle"]}}],
    )

    plan = plan_job(job, workspace)

    assert plan.actions[0].paths == (
        PurePosixPath("one.txt"),
        PurePosixPath("two.txt"),
    )
    assert [change.content for change in plan.file_changes] == [
        b"new one\n",
        b"new two\n",
    ]
    assert (workspace.root / "one.txt").read_text(encoding="utf-8") == "old one\n"


@pytest.mark.parametrize(
    ("diff", "code"),
    (
        ("not a diff\n", PlanningErrorCode.DIFF_INVALID),
        (
            "--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1 @@\n+new\n",
            PlanningErrorCode.DIFF_PATH_INVALID,
        ),
        (
            "--- a/one.txt\n+++ b/two.txt\n@@ -1 +1 @@\n-old\n+new\n",
            PlanningErrorCode.DIFF_PATH_INVALID,
        ),
        (
            "--- a/one.txt\n+++ b/one.txt\n@@ -1 +1 @@\n-wrong\n+new\n",
            PlanningErrorCode.DIFF_HUNK_MISMATCH,
        ),
        (
            "Binary files a/one.txt and b/one.txt differ\n",
            PlanningErrorCode.DIFF_BINARY_FORBIDDEN,
        ),
        ("index 123..456 100644\n", PlanningErrorCode.DIFF_INVALID),
        (
            "--- /one.txt\n+++ /one.txt\n@@ -1 +1 @@\n-old\n+new\n",
            PlanningErrorCode.DIFF_PATH_INVALID,
        ),
    ),
)
def test_unified_diff_rejections_have_stable_codes(
    workspace: Workspace,
    diff: str,
    code: PlanningErrorCode,
) -> None:
    (workspace.root / "one.txt").write_text("old\n", encoding="utf-8")
    job = make_job(
        actions=[{"apply_diff": {"diff": diff}}],
        checks=[{"import_check": {"modules": ["patchshuttle"]}}],
    )

    with pytest.raises(PlanningError) as caught:
        plan_job(job, workspace)

    assert caught.value.code is code


@pytest.mark.parametrize(
    "check",
    (
        {"compileall": {"paths": ["src"]}},
        {"pytest": {"paths": ["tests"], "args": ["-q", "--tb=short"]}},
        {"unittest": {"discover": "tests", "pattern": "test_*.py"}},
        {"django_check": {"manage_py": "manage.py"}},
        {"django_migrations_check": {"manage_py": "manage.py"}},
        {"django_test": {"manage_py": "manage.py", "labels": ["app.tests"]}},
        {"import_check": {"modules": ["patchshuttle"]}},
        {"profile": {"name": "project_tests"}},
    ),
)
def test_every_check_kind_is_planned(
    workspace: Workspace,
    check: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (workspace.root / "src").mkdir(exist_ok=True)
    (workspace.root / "tests").mkdir(exist_ok=True)
    (workspace.root / "manage.py").write_text("pass\n", encoding="utf-8")
    profile = workspace.config.checks.profiles.get("project_tests")
    if profile is None:
        from patchshuttle.config import CheckProfileSettings

        profiles = {
            "project_tests": CheckProfileSettings(argv=("{python}", "-m", "pytest"))
        }
        checks_config = workspace.config.checks.model_copy(
            update={"profiles": profiles}
        )
        workspace = Workspace(
            root=workspace.root,
            config=workspace.config.model_copy(update={"checks": checks_config}),
        )
    job = make_job(job_id="VERIFY-001", kind="verify", checks=[check])
    if next(iter(check)).startswith("django_"):
        monkeypatch.setattr(planner_module, "find_spec", lambda name: object())

    plan = plan_job(job, workspace)

    assert plan.checks[0].id == "check_001"
    assert plan.checks[0].name == next(iter(check))
    assert plan.requires_confirmation is True
    assert plan.backup_destination is None


def test_patch_check_can_target_a_virtual_file(workspace: Workspace) -> None:
    job = make_job(
        actions=[{"create_file": {"path": "module.py", "content": "pass\n"}}],
        checks=[{"compileall": {"paths": ["module.py"]}}],
    )

    plan = plan_job(job, workspace)

    assert plan.checks[0].paths == (PurePosixPath("module.py"),)


def test_local_policy_can_disable_patch_confirmation(workspace: Workspace) -> None:
    execution = workspace.config.execution.model_copy(update={"confirm": False})
    configured = Workspace(
        root=workspace.root,
        config=workspace.config.model_copy(update={"execution": execution}),
    )
    job = make_job(
        actions=[{"create_file": {"path": "module.py", "content": "pass\n"}}],
        checks=[{"compileall": {"paths": ["module.py"]}}],
    )

    plan = plan_job(job, configured)

    assert plan.requires_confirmation is False


def test_check_policy_rejects_missing_profile_forbidden_pytest_arg_and_bad_type(
    workspace: Workspace,
) -> None:
    (workspace.root / "file.txt").write_text("file\n", encoding="utf-8")
    cases = (
        (
            {"profile": {"name": "missing"}},
            PlanningErrorCode.CHECK_PROFILE_NOT_FOUND,
        ),
        (
            {"pytest": {"args": ["--basetemp=outside"]}},
            PlanningErrorCode.PYTEST_ARGUMENT_FORBIDDEN,
        ),
        (
            {"compileall": {"paths": ["missing"]}},
            PlanningErrorCode.CHECK_PATH_NOT_FOUND,
        ),
        (
            {"unittest": {"discover": "file.txt", "pattern": "test_*.py"}},
            PlanningErrorCode.TARGET_TYPE_INVALID,
        ),
    )
    for check, code in cases:
        job = make_job(job_id="VERIFY-001", kind="verify", checks=[check])
        with pytest.raises(PlanningError) as caught:
            plan_job(job, workspace)
        assert caught.value.code is code
        assert caught.value.item_id == "check_001"


def test_check_rejects_ignored_path(workspace: Workspace) -> None:
    ignored = workspace.root / ".pytest_cache"
    ignored.mkdir()
    job = make_job(
        job_id="VERIFY-001",
        kind="verify",
        checks=[{"compileall": {"paths": [".pytest_cache"]}}],
    )

    with pytest.raises(PlanningError) as caught:
        plan_job(job, workspace)

    assert caught.value.code is PlanningErrorCode.PATH_IGNORED


def test_django_label_validation_precedes_execution(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (workspace.root / "manage.py").write_text("pass\n", encoding="utf-8")
    monkeypatch.setattr(planner_module, "find_spec", lambda name: object())
    job = make_job(
        job_id="VERIFY-001",
        kind="verify",
        checks=[
            {
                "django_test": {
                    "manage_py": "manage.py",
                    "labels": ["--parallel"],
                }
            }
        ],
    )

    with pytest.raises(PlanningError) as caught:
        plan_job(job, workspace)

    assert caught.value.code is PlanningErrorCode.CHECK_ARGUMENT_INVALID


def test_missing_or_broken_check_dependency_is_reported(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = make_job(
        job_id="VERIFY-001",
        kind="verify",
        checks=[{"pytest": {"args": ["-q"]}}],
    )
    monkeypatch.setattr(planner_module, "find_spec", lambda name: None)

    with pytest.raises(PlanningError) as missing:
        plan_job(job, workspace)
    assert missing.value.code is PlanningErrorCode.DEPENDENCY_NOT_AVAILABLE

    def broken_find_spec(name: str):
        raise ImportError("broken package metadata")

    monkeypatch.setattr(planner_module, "find_spec", broken_find_spec)
    with pytest.raises(PlanningError) as broken:
        plan_job(job, workspace)
    assert broken.value.code is PlanningErrorCode.DEPENDENCY_NOT_AVAILABLE


@pytest.mark.parametrize("missing_name", ("isort", "black"))
def test_missing_formatter_dependency_is_reported_during_plan(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
    missing_name: str,
) -> None:
    original_find_spec = planner_module.find_spec

    def selective_find_spec(name: str):
        return None if name == missing_name else original_find_spec(name)

    monkeypatch.setattr(planner_module, "find_spec", selective_find_spec)
    job = make_job(
        actions=[{"create_file": {"path": "module.py", "content": "pass\n"}}],
        checks=[{"compileall": {"paths": ["module.py"]}}],
    )

    with pytest.raises(PlanningError) as caught:
        plan_job(job, workspace)

    assert caught.value.code is PlanningErrorCode.DEPENDENCY_NOT_AVAILABLE
    assert caught.value.item_id == "formatting"


def test_missing_profile_command_is_reported(
    workspace: Workspace,
) -> None:
    from patchshuttle.config import CheckProfileSettings

    profiles = {
        "missing_command": CheckProfileSettings(
            argv=("definitely-missing-patchshuttle-command",)
        )
    }
    checks = workspace.config.checks.model_copy(update={"profiles": profiles})
    configured = Workspace(
        root=workspace.root,
        config=workspace.config.model_copy(update={"checks": checks}),
    )
    job = make_job(
        job_id="VERIFY-001",
        kind="verify",
        checks=[{"profile": {"name": "missing_command"}}],
    )

    with pytest.raises(PlanningError) as caught:
        plan_job(job, configured)

    assert caught.value.code is PlanningErrorCode.DEPENDENCY_NOT_AVAILABLE


@pytest.mark.parametrize("use_absolute", (False, True))
def test_relative_and_absolute_profile_commands_are_resolved(
    workspace: Workspace,
    use_absolute: bool,
) -> None:
    from patchshuttle.config import CheckProfileSettings

    tools = workspace.root / "tools"
    tools.mkdir()
    command_path = tools / "check-runner"
    command_path.write_text("local command placeholder\n", encoding="utf-8")
    command = str(command_path) if use_absolute else "tools/check-runner"
    profiles = {"local_command": CheckProfileSettings(argv=(command, "--check"))}
    checks = workspace.config.checks.model_copy(update={"profiles": profiles})
    configured = Workspace(
        root=workspace.root,
        config=workspace.config.model_copy(update={"checks": checks}),
    )
    job = make_job(
        job_id="VERIFY-001",
        kind="verify",
        checks=[{"profile": {"name": "local_command"}}],
    )

    plan = plan_job(job, configured)

    assert plan.checks[0].name == "profile"


@pytest.mark.parametrize(
    "argument",
    ("--maxfail=2", "--capture=sys", "--capture=tee-sys"),
)
def test_documented_pytest_value_arguments_are_allowed(
    workspace: Workspace,
    argument: str,
) -> None:
    job = make_job(
        job_id="VERIFY-001",
        kind="verify",
        checks=[{"pytest": {"args": [argument]}}],
    )

    assert plan_job(job, workspace).checks[0].name == "pytest"


@pytest.mark.parametrize("argument", ("--maxfail=0", "--maxfail=x", "--capture=x"))
def test_invalid_pytest_value_arguments_are_rejected(
    workspace: Workspace,
    argument: str,
) -> None:
    job = make_job(
        job_id="VERIFY-001",
        kind="verify",
        checks=[{"pytest": {"args": [argument]}}],
    )

    with pytest.raises(PlanningError) as caught:
        plan_job(job, workspace)

    assert caught.value.code is PlanningErrorCode.PYTEST_ARGUMENT_FORBIDDEN


def test_disabled_formatting_produces_no_scope(workspace: Workspace) -> None:
    formatting = workspace.config.formatting.model_copy(update={"enabled": False})
    configured = Workspace(
        root=workspace.root,
        config=workspace.config.model_copy(update={"formatting": formatting}),
    )
    job = make_job(
        actions=[{"create_file": {"path": "module.py", "content": "pass\n"}}],
        checks=[{"compileall": {"paths": ["module.py"]}}],
    )

    assert plan_job(job, configured).formatting_targets == ()


def test_file_metadata_read_content_read_and_fingerprint_failures_are_stable(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = workspace.root / "target.txt"
    target.write_text("text\n", encoding="utf-8")
    job = make_job(
        job_id="AUDIT-001",
        kind="audit",
        actions=[{"read": {"path": "target.txt"}}],
    )
    original_lstat = Path.lstat
    calls = 0

    def metadata_race(self: Path):
        nonlocal calls
        if self == target:
            calls += 1
            if calls == 3:
                raise OSError("metadata unavailable")
        return original_lstat(self)

    monkeypatch.setattr(Path, "lstat", metadata_race)
    with pytest.raises(PlanningError) as metadata_error:
        plan_job(job, workspace)
    assert metadata_error.value.code is PlanningErrorCode.FILE_READ_FAILED

    monkeypatch.setattr(Path, "lstat", original_lstat)
    original_read_bytes = Path.read_bytes

    def failed_read(self: Path) -> bytes:
        if self == target:
            raise OSError("read unavailable")
        return original_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", failed_read)
    with pytest.raises(PlanningError) as read_error:
        plan_job(job, workspace)
    assert read_error.value.code is PlanningErrorCode.FILE_READ_FAILED

    monkeypatch.setattr(Path, "read_bytes", original_read_bytes)
    calls = 0

    def fingerprint_race(self: Path):
        nonlocal calls
        if self == target:
            calls += 1
            if calls == 2:
                raise OSError("fingerprint unavailable")
        return original_lstat(self)

    monkeypatch.setattr(Path, "lstat", fingerprint_race)
    info_job = make_job(
        job_id="AUDIT-002",
        kind="audit",
        actions=[{"file_info": {"path": "target.txt"}}],
    )
    with pytest.raises(PlanningError) as fingerprint_error:
        plan_job(info_job, workspace)
    assert fingerprint_error.value.code is PlanningErrorCode.FILE_READ_FAILED


def test_path_change_during_file_read_is_rejected(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = workspace.root / "target.txt"
    target.write_text("text\n", encoding="utf-8")
    original_resolve = Policy.resolve
    calls = 0

    def changed_resolve(self: Policy, value, **kwargs):
        nonlocal calls
        result = original_resolve(self, value, **kwargs)
        if result.relative == PurePosixPath("target.txt"):
            calls += 1
            if calls == 2:
                return WorkspacePath(
                    relative=result.relative,
                    absolute=workspace.root / "other.txt",
                    kind=PathKind.FILE,
                )
        return result

    monkeypatch.setattr(Policy, "resolve", changed_resolve)
    job = make_job(
        job_id="AUDIT-001",
        kind="audit",
        actions=[{"read": {"path": "target.txt"}}],
    )

    with pytest.raises(PlanningError) as caught:
        plan_job(job, workspace)

    assert caught.value.code is PlanningErrorCode.FILE_READ_FAILED


def test_parent_type_race_during_creation_is_rejected(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_resolve = Policy.resolve

    def raced_resolve(self: Policy, value, **kwargs):
        result = original_resolve(self, value, **kwargs)
        if self.normalize(value) == PurePosixPath("src"):
            return WorkspacePath(
                relative=PurePosixPath("src"),
                absolute=workspace.root / "src",
                kind=PathKind.FILE,
            )
        return result

    monkeypatch.setattr(Policy, "resolve", raced_resolve)
    job = make_job(
        actions=[{"create_file": {"path": "src/module.py", "content": "pass\n"}}],
        checks=[{"import_check": {"modules": ["patchshuttle"]}}],
    )

    with pytest.raises(PlanningError) as caught:
        plan_job(job, workspace)

    assert caught.value.code is PlanningErrorCode.TARGET_TYPE_INVALID


def test_planning_error_string_contains_code_item_and_path() -> None:
    error = PlanningError(
        PlanningErrorCode.TARGET_TYPE_INVALID,
        "expected a directory",
        item_id="action_001",
        path="README.md",
    )

    assert str(error) == (
        "[TARGET_TYPE_INVALID] action_001 README.md: expected a directory"
    )
