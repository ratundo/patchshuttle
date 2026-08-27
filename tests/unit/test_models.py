"""Contract tests for immutable job, action, and check models."""

from copy import deepcopy
from typing import Any

import pytest
from pydantic import ValidationError

from patchshuttle import Action, Check, Job, JobKind

PROJECT_ID = "PSH-8F41C2A73D905E61"

AUDIT_ACTIONS: tuple[tuple[dict[str, Any], str], ...] = (
    ({"tree": {}}, "tree"),
    ({"read": {"path": "README.md"}}, "read"),
    ({"search": {"text": "TODO"}}, "search"),
    ({"search_context": {"text": "TODO"}}, "search_context"),
    (
        {"read_symbol": {"path": "src/example.py", "symbol": "Service.run"}},
        "read_symbol",
    ),
    (
        {"python_structure": {"path": "src", "compact": True}},
        "python_structure",
    ),
    ({"find_files": {"glob": "*.py"}}, "find_files"),
    ({"file_info": {"path": "pyproject.toml"}}, "file_info"),
    ({"hash": {"path": "README.md"}}, "hash"),
    (
        {
            "hash_range": {
                "path": "README.md",
                "start_line": 1,
                "end_line": 2,
            }
        },
        "hash_range",
    ),
    ({"git_status": {}}, "git_status"),
    ({"environment": {}}, "environment"),
)

CHANGE_ACTIONS: tuple[tuple[dict[str, Any], str], ...] = (
    ({"create_directory": {"path": "src/example"}}, "create_directory"),
    (
        {
            "create_file": {
                "path": "src/example/greeting.py",
                "content": "VALUE = 1\n",
            }
        },
        "create_file",
    ),
    (
        {
            "replace_exact": {
                "path": "src/example.py",
                "old": "before",
                "new": "after",
            }
        },
        "replace_exact",
    ),
    (
        {
            "replace_symbol": {
                "path": "src/example.py",
                "symbol": "Service.run",
                "expected_sha256": "a" * 64,
                "new_content": "    def run(self):\n        return 2\n",
            }
        },
        "replace_symbol",
    ),
    (
        {
            "insert_before": {
                "path": "src/example.py",
                "anchor": "def run():",
                "content": "# generated\n",
            }
        },
        "insert_before",
    ),
    (
        {
            "insert_after": {
                "path": "src/example.py",
                "anchor": "def run():",
                "content": "\n    return None",
            }
        },
        "insert_after",
    ),
    (
        {
            "delete_exact": {
                "path": "src/example.py",
                "text": "# obsolete\n",
            }
        },
        "delete_exact",
    ),
    (
        {
            "replace_range": {
                "path": "src/example.py",
                "start_line": 2,
                "end_line": 3,
                "expected_content": "before\nblock\n",
                "new_content": "after\nblock\n",
            }
        },
        "replace_range",
    ),
    (
        {
            "delete_range": {
                "path": "src/example.py",
                "start_line": 2,
                "end_line": 3,
                "expected_sha256": "a" * 64,
            }
        },
        "delete_range",
    ),
    (
        {
            "insert_at_line": {
                "path": "src/example.py",
                "line": 2,
                "position": "after",
                "content": "inserted\n",
                "expected_content": "anchor\n",
            }
        },
        "insert_at_line",
    ),
    (
        {
            "apply_diff": {
                "diff": "--- a/src/example.py\n+++ b/src/example.py\n",
            }
        },
        "apply_diff",
    ),
)

CHECKS: tuple[tuple[dict[str, Any], str], ...] = (
    ({"compileall": {"paths": ["src"]}}, "compileall"),
    ({"ruff": {}}, "ruff"),
    (
        {
            "pytest": {
                "paths": ["tests"],
                "args": ["-q"],
                "timeout_seconds": 300,
            }
        },
        "pytest",
    ),
    (
        {"unittest": {"discover": "tests", "pattern": "test_*.py"}},
        "unittest",
    ),
    ({"django_check": {"manage_py": "manage.py"}}, "django_check"),
    (
        {"django_migrations_check": {"manage_py": "manage.py"}},
        "django_migrations_check",
    ),
    (
        {
            "django_test": {
                "manage_py": "manage.py",
                "labels": ["clients.tests"],
            }
        },
        "django_test",
    ),
    (
        {
            "django_import_check": {
                "manage_py": "manage.py",
                "modules": ["clients.models", "email_client.views"],
            }
        },
        "django_import_check",
    ),
    (
        {"import_check": {"modules": ["patchshuttle", "patchshuttle.cli"]}},
        "import_check",
    ),
    ({"profile": {"name": "manager_tests"}}, "profile"),
)


@pytest.mark.parametrize(("payload", "name"), AUDIT_ACTIONS + CHANGE_ACTIONS)
def test_every_action_model_accepts_its_documented_shape(
    payload: dict[str, Any], name: str
) -> None:
    action = Action.model_validate(payload)

    assert action.name == name
    assert list(action.model_dump(mode="json")) == [name]
    assert Action.model_validate(action.model_dump()) == action


@pytest.mark.parametrize(("payload", "name"), AUDIT_ACTIONS)
def test_audit_actions_are_classified(payload: dict[str, Any], name: str) -> None:
    action = Action.model_validate(payload)

    assert action.is_audit is True
    assert action.is_change is False


@pytest.mark.parametrize(("payload", "name"), CHANGE_ACTIONS)
def test_change_actions_are_classified(payload: dict[str, Any], name: str) -> None:
    action = Action.model_validate(payload)

    assert action.is_audit is False
    assert action.is_change is True


@pytest.mark.parametrize(("payload", "name"), AUDIT_ACTIONS + CHANGE_ACTIONS)
def test_every_action_model_rejects_unknown_parameter_fields(
    payload: dict[str, Any], name: str
) -> None:
    invalid = deepcopy(payload)
    invalid[name]["unexpected"] = True

    with pytest.raises(ValidationError):
        Action.model_validate(invalid)


@pytest.mark.parametrize(("payload", "name"), CHECKS)
def test_every_check_model_accepts_its_documented_shape(
    payload: dict[str, Any], name: str
) -> None:
    check = Check.model_validate(payload)

    assert check.name == name
    assert check.parameters == getattr(check.root, name)
    assert list(check.model_dump(mode="json")) == [name]
    assert Check.model_validate(check.model_dump()) == check


@pytest.mark.parametrize(("payload", "name"), CHECKS)
def test_every_check_model_rejects_unknown_parameter_fields(
    payload: dict[str, Any], name: str
) -> None:
    invalid = deepcopy(payload)
    invalid[name]["unexpected"] = True

    with pytest.raises(ValidationError):
        Check.model_validate(invalid)


def test_named_entries_require_exactly_one_known_name() -> None:
    with pytest.raises(ValidationError):
        Action.model_validate({"unknown": {}})

    with pytest.raises(ValidationError):
        Action.model_validate(
            {"read": {"path": "README.md"}, "hash": {"path": "README.md"}}
        )

    with pytest.raises(ValidationError):
        Check.model_validate({"unknown": {}})

    with pytest.raises(ValidationError):
        Check.model_validate(
            {"compileall": {"paths": ["src"]}, "pytest": {"paths": ["tests"]}}
        )


def test_action_and_check_entries_must_be_mappings() -> None:
    with pytest.raises(ValidationError):
        Action.model_validate("read")

    with pytest.raises(ValidationError):
        Check.model_validate(["pytest"])


@pytest.mark.parametrize(
    "payload",
    (
        {"tree": {"depth": 0}},
        {"tree": {"depth": 11}},
        {"tree": {"depth": "4"}},
        {"tree": {"include_hidden": 1}},
        {"read": {"path": "README.md", "start_line": 0}},
        {"read": {"path": "README.md", "start_line": 10, "end_line": 9}},
        {"search_context": {"text": "TODO", "before": -1}},
        {"search_context": {"text": "TODO", "after": 501}},
        {"read_symbol": {"path": "example.py", "symbol": "Service..run"}},
        {"read_symbol": {"path": "example.py", "symbol": "run", "max_bytes": 0}},
        {
            "replace_symbol": {
                "path": "example.py",
                "symbol": "Service..run",
                "expected_sha256": "a" * 64,
                "new_content": "    def run(self):\n        return 2\n",
            }
        },
        {
            "replace_symbol": {
                "path": "example.py",
                "symbol": "Service.run",
                "expected_sha256": "not-a-sha256",
                "new_content": "    def run(self):\n        return 2\n",
            }
        },
        {
            "hash_range": {
                "path": "README.md",
                "start_line": 2,
                "end_line": 1,
            }
        },
        {
            "replace_exact": {
                "path": "file.py",
                "old": "",
                "new": "value",
            }
        },
        {
            "insert_before": {
                "path": "file.py",
                "anchor": "",
                "content": "value",
            }
        },
        {"delete_exact": {"path": "file.py", "text": ""}},
        {
            "replace_range": {
                "path": "file.py",
                "start_line": 1,
                "end_line": 1,
                "new_content": "new",
            }
        },
        {
            "delete_range": {
                "path": "file.py",
                "start_line": 0,
                "end_line": 1,
                "expected_content": "old",
            }
        },
        {
            "delete_range": {
                "path": "file.py",
                "start_line": 2,
                "end_line": 1,
                "expected_content": "old",
            }
        },
        {
            "delete_range": {
                "path": "file.py",
                "start_line": 1,
                "end_line": 1,
                "expected_sha256": "not-a-sha256",
            }
        },
        {
            "insert_at_line": {
                "path": "file.py",
                "line": 1,
                "position": "middle",
                "content": "new",
                "expected_content": "old",
            }
        },
        {
            "insert_at_line": {
                "path": "file.py",
                "line": 1,
                "position": "before",
                "content": "",
                "expected_content": "old",
            }
        },
        {
            "insert_at_line": {
                "path": "file.py",
                "line": 1,
                "position": "before",
                "content": "new",
            }
        },
        {"apply_diff": {"diff": "", "strip": 1}},
        {"apply_diff": {"diff": "diff", "strip": 3}},
    ),
)
def test_action_parameter_constraints(payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Action.model_validate(payload)


@pytest.mark.parametrize(
    "payload",
    (
        {"compileall": {"paths": []}},
        {"compileall": {"paths": ["src"], "quiet": True}},
        {"pytest": {"timeout_seconds": 0}},
        {"import_check": {"modules": []}},
        {"import_check": {"modules": ["patchshuttle;exit()"]}},
        {"import_check": {"modules": ["patchshuttle..cli"]}},
        {"django_import_check": {"manage_py": "manage.py", "modules": []}},
        {
            "django_import_check": {
                "manage_py": "manage.py",
                "modules": ["clients.models;exit()"],
            }
        },
        {"profile": {"name": ""}},
    ),
)
def test_check_parameter_constraints(payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Check.model_validate(payload)


def test_module_import_checks_have_bounded_argument_lists() -> None:
    with pytest.raises(ValidationError):
        Check.model_validate(
            {"import_check": {"modules": [f"module_{index}" for index in range(101)]}}
        )

    with pytest.raises(ValidationError):
        Check.model_validate(
            {"django_import_check": {"manage_py": "manage.py", "modules": ["m" * 8001]}}
        )

    compileall = Check.model_validate(
        {"compileall": {"paths": [f"src/path_{index}" for index in range(101)]}}
    )
    assert len(compileall.parameters.paths) == 101


def test_documented_action_defaults_are_stable() -> None:
    tree = Action.model_validate({"tree": {}}).parameters
    read = Action.model_validate({"read": {"path": "README.md"}}).parameters
    create_file = Action.model_validate(
        {"create_file": {"path": "empty.txt", "content": ""}}
    ).parameters
    apply_diff = Action.model_validate(
        {"apply_diff": {"diff": "--- a/file\n+++ b/file\n"}}
    ).parameters
    hash_range = Action.model_validate(
        {
            "hash_range": {
                "path": "README.md",
                "start_line": 1,
                "end_line": 1,
            }
        }
    ).parameters
    guarded = Action.model_validate(
        {
            "delete_range": {
                "path": "README.md",
                "start_line": 1,
                "end_line": 1,
                "expected_sha256": "A" * 64,
            }
        }
    ).parameters

    assert tree.model_dump() == {
        "path": ".",
        "depth": 4,
        "max_entries": 500,
        "include_hidden": False,
    }
    assert read.model_dump() == {
        "path": "README.md",
        "start_line": 1,
        "end_line": None,
        "max_bytes": None,
    }
    assert create_file.model_dump() == {
        "path": "empty.txt",
        "content": "",
        "encoding": "utf-8",
        "newline": "lf",
    }
    assert apply_diff.model_dump() == {
        "diff": "--- a/file\n+++ b/file\n",
        "strip": 1,
    }
    assert hash_range.model_dump() == {
        "path": "README.md",
        "start_line": 1,
        "end_line": 1,
        "algorithm": "sha256",
    }
    assert guarded.expected_sha256 == "a" * 64


def test_context_action_defaults_are_stable() -> None:
    search_context = Action.model_validate(
        {"search_context": {"text": "TODO"}}
    ).parameters
    read_symbol = Action.model_validate(
        {"read_symbol": {"path": "example.py", "symbol": "Service.run"}}
    ).parameters

    assert search_context.model_dump() == {
        "path": ".",
        "text": "TODO",
        "glob": None,
        "case_sensitive": True,
        "max_results": 200,
        "before": 3,
        "after": 3,
    }
    assert read_symbol.model_dump() == {
        "path": "example.py",
        "symbol": "Service.run",
        "max_bytes": None,
    }


def test_audit_job_requires_only_audit_actions() -> None:
    job = Job(
        protocol=1,
        project_id=PROJECT_ID,
        id="AUDIT-001",
        kind="audit",
        actions=[Action.model_validate({"tree": {}})],
    )

    assert job.kind is JobKind.AUDIT
    assert isinstance(job.actions, tuple)
    assert job.checks == ()

    with pytest.raises(ValidationError):
        Job(
            protocol=1,
            project_id=PROJECT_ID,
            id="AUDIT-002",
            kind="audit",
            actions=[],
        )

    with pytest.raises(ValidationError):
        Job(
            protocol=1,
            project_id=PROJECT_ID,
            id="AUDIT-003",
            kind="audit",
            actions=[CHANGE_ACTIONS[0][0]],
        )

    with pytest.raises(ValidationError):
        Job(
            protocol=1,
            project_id=PROJECT_ID,
            id="AUDIT-004",
            kind="audit",
            actions=[AUDIT_ACTIONS[0][0]],
            checks=[CHECKS[0][0]],
        )


def test_patch_job_requires_change_actions() -> None:
    job = Job(
        protocol=1,
        project_id=PROJECT_ID,
        id="PATCH-001",
        kind="patch",
        title="Create a package",
        actions=[CHANGE_ACTIONS[0][0], CHANGE_ACTIONS[1][0]],
        checks=[CHECKS[0][0]],
    )

    assert job.kind is JobKind.PATCH
    assert len(job.actions) == 2
    assert len(job.checks) == 1

    with pytest.raises(ValidationError):
        Job(
            protocol=1,
            project_id=PROJECT_ID,
            id="PATCH-002",
            kind="patch",
            actions=[],
            checks=[CHECKS[0][0]],
        )

    with pytest.raises(ValidationError):
        Job(
            protocol=1,
            project_id=PROJECT_ID,
            id="PATCH-003",
            kind="patch",
            actions=[AUDIT_ACTIONS[0][0]],
            checks=[CHECKS[0][0]],
        )


def test_patch_checks_remain_a_local_policy_decision() -> None:
    job = Job(
        protocol=1,
        project_id=PROJECT_ID,
        id="PATCH-004",
        kind="patch",
        actions=[CHANGE_ACTIONS[0][0]],
    )

    assert job.checks == ()


def test_verify_job_requires_checks_and_forbids_actions() -> None:
    job = Job(
        protocol=1,
        project_id=PROJECT_ID,
        id="VERIFY-001",
        kind="verify",
        checks=[CHECKS[1][0]],
    )

    assert job.kind is JobKind.VERIFY
    assert job.actions == ()

    with pytest.raises(ValidationError):
        Job(
            protocol=1,
            project_id=PROJECT_ID,
            id="VERIFY-002",
            kind="verify",
        )

    with pytest.raises(ValidationError):
        Job(
            protocol=1,
            project_id=PROJECT_ID,
            id="VERIFY-003",
            kind="verify",
            actions=[AUDIT_ACTIONS[0][0]],
            checks=[CHECKS[0][0]],
        )


@pytest.mark.parametrize(
    "job_id",
    (
        "AB",
        "patch-001",
        "1PATCH",
        "PATCH 001",
        "A" * 65,
    ),
)
def test_job_id_format_is_enforced(job_id: str) -> None:
    with pytest.raises(ValidationError):
        Job(
            protocol=1,
            project_id=PROJECT_ID,
            id=job_id,
            kind="audit",
            actions=[AUDIT_ACTIONS[0][0]],
        )


@pytest.mark.parametrize(
    "project_id",
    (
        "PSH-8F41C2A73D905E6",
        "PSH-8F41C2A73D905E611",
        "psh-8F41C2A73D905E61",
        "PSH-8f41C2A73D905E61",
        "PROJECT-8F41C2A73D905E61",
    ),
)
def test_project_id_format_is_enforced(project_id: str) -> None:
    with pytest.raises(ValidationError):
        Job(
            protocol=1,
            project_id=project_id,
            id="AUDIT-001",
            kind="audit",
            actions=[AUDIT_ACTIONS[0][0]],
        )


@pytest.mark.parametrize("protocol", (0, 2, "1", True))
def test_protocol_must_be_the_strict_integer_one(protocol: object) -> None:
    with pytest.raises(ValidationError):
        Job(
            protocol=protocol,
            project_id=PROJECT_ID,
            id="AUDIT-001",
            kind="audit",
            actions=[AUDIT_ACTIONS[0][0]],
        )


def test_models_are_deeply_immutable() -> None:
    job = Job(
        protocol=1,
        project_id=PROJECT_ID,
        id="AUDIT-001",
        kind="audit",
        actions=[AUDIT_ACTIONS[0][0]],
    )

    with pytest.raises(ValidationError):
        job.title = "Changed"

    with pytest.raises(ValidationError):
        job.actions[0].parameters.path = "src"


def test_job_rejects_unknown_top_level_fields() -> None:
    with pytest.raises(ValidationError):
        Job.model_validate(
            {
                "protocol": 1,
                "project_id": PROJECT_ID,
                "id": "AUDIT-001",
                "kind": "audit",
                "actions": [AUDIT_ACTIONS[0][0]],
                "command": "rm -rf project",
            }
        )


def test_job_round_trip_and_json_schema() -> None:
    payload = {
        "protocol": 1,
        "project_id": PROJECT_ID,
        "id": "PATCH-001",
        "kind": "patch",
        "title": "Create a file",
        "description": "Create one guarded text file.",
        "actions": [CHANGE_ACTIONS[1][0]],
        "checks": [CHECKS[0][0]],
    }

    job = Job.model_validate(payload)
    dumped = job.model_dump(mode="json", exclude_defaults=True)
    schema = Job.model_json_schema()

    assert dumped == payload
    assert Job.model_validate(dumped) == job
    assert schema["properties"]["protocol"]["const"] == 1
    assert schema["additionalProperties"] is False
