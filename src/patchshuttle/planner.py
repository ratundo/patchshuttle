"""Immutable plans produced by read-only workspace inspection."""

from __future__ import annotations

import codecs
import hashlib
import json
import re
import shutil
import sys
from dataclasses import dataclass, field
from enum import Enum
from importlib.util import find_spec
from os import PathLike
from pathlib import Path, PurePosixPath
from typing import cast

from patchshuttle._diff import apply_file_diff, parse_unified_diff
from patchshuttle.errors import PlanningError, PlanningErrorCode
from patchshuttle.models import Action, Check, Job, JobKind
from patchshuttle.policy import PathKind, Policy, WorkspacePath
from patchshuttle.workspace import Workspace, discover_workspace


class ActionDisposition(str, Enum):
    """Read-only conclusion for one requested action."""

    INSPECT = "INSPECT"
    CREATE = "CREATE"
    MODIFY = "MODIFY"
    NO_CHANGE = "NO_CHANGE"


class FileDisposition(str, Enum):
    """Net file operation required by a complete plan."""

    CREATE = "CREATE"
    MODIFY = "MODIFY"


class NewlineStyle(str, Enum):
    """Newline style preserved or requested for a planned text file."""

    LF = "lf"
    CRLF = "crlf"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class PlannedAction:
    """One sequential action and its dry-run disposition."""

    id: str
    name: str
    disposition: ActionDisposition
    paths: tuple[PurePosixPath, ...] = ()
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class PlannedCheck:
    """One requested controlled check and its validated workspace paths."""

    id: str
    name: str
    paths: tuple[PurePosixPath, ...] = ()


@dataclass(frozen=True, slots=True)
class PlannedFileChange:
    """The final bytes and fingerprints for one net file change."""

    path: PurePosixPath
    disposition: FileDisposition
    before_sha256: str | None
    after_sha256: str
    before_size: int | None
    after_size: int
    encoding: str
    newline: NewlineStyle
    content: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class PathFingerprint:
    """Read-only metadata retained for future plan revalidation."""

    path: PurePosixPath
    kind: PathKind
    size: int
    modified_ns: int
    sha256: str | None = None


@dataclass(frozen=True, slots=True)
class Plan:
    """A complete immutable read-only plan for one validated job."""

    workspace: Workspace = field(repr=False)
    job: Job
    job_hash: str
    actions: tuple[PlannedAction, ...]
    checks: tuple[PlannedCheck, ...]
    file_changes: tuple[PlannedFileChange, ...]
    directories_to_create: tuple[PurePosixPath, ...]
    formatting_targets: tuple[PurePosixPath, ...]
    fingerprints: tuple[PathFingerprint, ...]
    protected_paths_passed: bool
    backup_destination: PurePosixPath | None
    auto_rollback: bool

    @property
    def files_to_create(self) -> tuple[PurePosixPath, ...]:
        return tuple(
            change.path
            for change in self.file_changes
            if change.disposition is FileDisposition.CREATE
        )

    @property
    def files_to_modify(self) -> tuple[PurePosixPath, ...]:
        return tuple(
            change.path
            for change in self.file_changes
            if change.disposition is FileDisposition.MODIFY
        )

    @property
    def requires_confirmation(self) -> bool:
        return (
            self.job.kind is not JobKind.AUDIT
            and self.workspace.config.execution.confirm
        )


@dataclass(slots=True)
class _TextState:
    path: PurePosixPath
    original_bytes: bytes | None
    current_bytes: bytes
    text: str
    encoding: str
    codec: str
    bom: bytes
    newline: NewlineStyle


_UTF32_LE_BOM = b"\xff\xfe\x00\x00"
_UTF32_BE_BOM = b"\x00\x00\xfe\xff"
_UTF16_LE_BOM = b"\xff\xfe"
_UTF16_BE_BOM = b"\xfe\xff"
_UTF8_BOM = b"\xef\xbb\xbf"
_PYTEST_EXACT_ARGS = frozenset(
    {
        "-q",
        "--quiet",
        "-v",
        "--verbose",
        "-x",
        "--exitfirst",
        "-s",
        "--disable-warnings",
        "--strict-config",
        "--strict-markers",
    }
)
_PYTEST_TB_VALUES = frozenset({"auto", "long", "short", "line", "native", "no"})
_PYTEST_CAPTURE_VALUES = frozenset({"fd", "sys", "no", "tee-sys"})
_DJANGO_LABEL = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")


def plan_job(
    job: Job,
    workspace: Workspace | str | PathLike[str] = ".",
) -> Plan:
    """Validate local authority and fully dry-run one immutable job."""

    resolved_workspace = (
        workspace if isinstance(workspace, Workspace) else discover_workspace(workspace)
    )
    resolved_workspace.require_project_id(job.project_id)
    return _Planner(job, resolved_workspace).build()


class _Planner:
    def __init__(self, job: Job, workspace: Workspace) -> None:
        self.job = job
        self.workspace = workspace
        self.policy = Policy(workspace)
        self.action_plans: list[PlannedAction] = []
        self.check_plans: list[PlannedCheck] = []
        self.files: dict[PurePosixPath, _TextState] = {}
        self.created_directories: dict[PurePosixPath, None] = {}
        self.fingerprints: dict[PurePosixPath, PathFingerprint] = {}
        self.max_file_bytes = workspace.config.execution.max_single_file_bytes

    def build(self) -> Plan:
        self._validate_job_policy()
        for index, action in enumerate(self.job.actions, start=1):
            item_id = f"action_{index:03d}"
            if self.job.kind is JobKind.AUDIT:
                self._plan_audit_action(action, item_id=item_id)
            else:
                self._plan_change_action(action, item_id=item_id)

        for index, check in enumerate(self.job.checks, start=1):
            self._plan_check(check, item_id=f"check_{index:03d}")

        file_changes = self._build_file_changes()
        formatting_targets = self._formatting_targets(file_changes)
        if formatting_targets:
            self._require_module("isort", item_id="formatting")
            self._require_module("black", item_id="formatting")
        backup_destination = (
            PurePosixPath(
                "patches",
                "backups",
                self.job.id,
                "<RUN_TIMESTAMP>",
            )
            if self.job.kind is JobKind.PATCH
            else None
        )
        return Plan(
            workspace=self.workspace,
            job=self.job,
            job_hash=normalized_job_hash(self.job),
            actions=tuple(self.action_plans),
            checks=tuple(self.check_plans),
            file_changes=file_changes,
            directories_to_create=tuple(self.created_directories),
            formatting_targets=formatting_targets,
            fingerprints=tuple(self.fingerprints.values()),
            protected_paths_passed=True,
            backup_destination=backup_destination,
            auto_rollback=(
                self.workspace.config.execution.auto_rollback
                if self.job.kind is JobKind.PATCH
                else False
            ),
        )

    def _validate_job_policy(self) -> None:
        action_limit = self.workspace.config.execution.max_actions
        if len(self.job.actions) > action_limit:
            raise PlanningError(
                PlanningErrorCode.ACTION_LIMIT_EXCEEDED,
                f"job has more than the configured {action_limit} action(s)",
            )
        if (
            self.job.kind is JobKind.PATCH
            and self.workspace.config.checks.require_at_least_one_for_patch
            and not self.job.checks
        ):
            raise PlanningError(
                PlanningErrorCode.PATCH_CHECK_REQUIRED,
                "local policy requires at least one check for a patch job",
            )

    def _plan_audit_action(self, action: Action, *, item_id: str) -> None:
        parameters = action.parameters
        paths: tuple[PurePosixPath, ...] = ()
        if action.name in {"tree", "find_files"}:
            target = self._audit_target(
                cast(str, parameters.path),
                item_id=item_id,
                expected=PathKind.DIRECTORY,
            )
            paths = (target.relative,)
        elif action.name == "read":
            requested_max = parameters.max_bytes
            if requested_max is not None and requested_max > self.max_file_bytes:
                raise self._error(
                    PlanningErrorCode.FILE_SIZE_LIMIT_EXCEEDED,
                    f"read max_bytes exceeds the configured {self.max_file_bytes}-byte limit",
                    item_id,
                    path=parameters.path,
                )
            target = self._audit_target(
                parameters.path,
                item_id=item_id,
                expected=PathKind.FILE,
            )
            raw = self._read_existing_file(target, item_id=item_id)
            self._decode_existing(raw, item_id=item_id, path=target.relative)
            paths = (target.relative,)
        elif action.name == "search":
            target = self._audit_target(parameters.path, item_id=item_id)
            if target.kind is PathKind.FILE:
                raw = self._read_existing_file(target, item_id=item_id)
                self._decode_existing(raw, item_id=item_id, path=target.relative)
            paths = (target.relative,)
        elif action.name in {"file_info", "hash"}:
            expected = PathKind.FILE if action.name == "hash" else None
            target = self._audit_target(
                parameters.path,
                item_id=item_id,
                expected=expected,
            )
            if action.name == "hash":
                self._read_existing_file(target, item_id=item_id)
            paths = (target.relative,)

        self.action_plans.append(
            PlannedAction(
                id=item_id,
                name=action.name,
                disposition=ActionDisposition.INSPECT,
                paths=paths,
            )
        )

    def _audit_target(
        self,
        path: str,
        *,
        item_id: str,
        expected: PathKind | None = None,
    ) -> WorkspacePath:
        relative = self.policy.normalize(path)
        if self.policy.is_ignored(relative):
            raise self._error(
                PlanningErrorCode.PATH_IGNORED,
                "audit target is ignored by local policy",
                item_id,
                path=relative.as_posix(),
            )
        target = self.policy.resolve(relative, allow_root=True)
        if expected is not None and target.kind is not expected:
            raise self._error(
                PlanningErrorCode.TARGET_TYPE_INVALID,
                f"expected a {expected.value}",
                item_id,
                path=relative.as_posix(),
            )
        self._record_fingerprint(target, item_id=item_id)
        return target

    def _plan_change_action(self, action: Action, *, item_id: str) -> None:
        if action.name == "create_directory":
            self._plan_create_directory(action, item_id=item_id)
        elif action.name == "create_file":
            self._plan_create_file(action, item_id=item_id)
        elif action.name in {
            "replace_exact",
            "insert_before",
            "insert_after",
            "delete_exact",
        }:
            self._plan_exact_edit(action, item_id=item_id)
        else:
            self._plan_apply_diff(action, item_id=item_id)

    def _plan_create_directory(self, action: Action, *, item_id: str) -> None:
        path = self.policy.normalize(action.parameters.path)
        target = self.policy.resolve(path, allow_missing=True)
        kind = self._virtual_kind(path, target, item_id=item_id)
        if kind is PathKind.FILE:
            raise self._error(
                PlanningErrorCode.TARGET_TYPE_INVALID,
                "create_directory target is an existing file",
                item_id,
                path=path.as_posix(),
            )
        if kind is PathKind.DIRECTORY:
            disposition = ActionDisposition.NO_CHANGE
            self._record_fingerprint(target, item_id=item_id)
        else:
            self._ensure_parent_directories(path, item_id=item_id)
            self.created_directories.setdefault(path, None)
            disposition = ActionDisposition.CREATE
        self.action_plans.append(
            PlannedAction(
                id=item_id,
                name=action.name,
                disposition=disposition,
                paths=(path,),
            )
        )

    def _plan_create_file(self, action: Action, *, item_id: str) -> None:
        parameters = action.parameters
        path = self.policy.normalize(parameters.path)
        target = self.policy.resolve(path, allow_missing=True)
        self._ensure_parent_directories(path, item_id=item_id)
        kind = self._virtual_kind(path, target, item_id=item_id)
        if kind is PathKind.DIRECTORY:
            raise self._error(
                PlanningErrorCode.TARGET_TYPE_INVALID,
                "create_file target is an existing directory",
                item_id,
                path=path.as_posix(),
            )

        new_state = self._new_file_state(
            path,
            parameters.content,
            parameters.encoding,
            NewlineStyle(parameters.newline),
            item_id=item_id,
        )
        if kind is PathKind.FILE:
            current = self._get_file(path, target, item_id=item_id)
            if current.current_bytes != new_state.current_bytes:
                raise self._error(
                    PlanningErrorCode.CREATE_FILE_CONFLICT,
                    "create_file target already exists with different content",
                    item_id,
                    path=path.as_posix(),
                )
            disposition = ActionDisposition.NO_CHANGE
        else:
            self.files[path] = new_state
            disposition = ActionDisposition.CREATE

        self.action_plans.append(
            PlannedAction(
                id=item_id,
                name=action.name,
                disposition=disposition,
                paths=(path,),
            )
        )

    def _plan_exact_edit(self, action: Action, *, item_id: str) -> None:
        parameters = action.parameters
        path = self.policy.normalize(parameters.path)
        target = self.policy.resolve(path, allow_missing=True)
        state = self._get_file(path, target, item_id=item_id)
        text = state.text

        if action.name == "replace_exact":
            old = _normalize_newlines(parameters.old)
            new = _normalize_newlines(parameters.new)
            actual = text.count(old)
            if actual == parameters.expected_count:
                updated = text.replace(old, new)
            elif actual == 0 and new and text.count(new) == parameters.expected_count:
                updated = text
            else:
                raise self._occurrence_error(
                    item_id,
                    path,
                    expected=parameters.expected_count,
                    actual=actual,
                )
        elif action.name in {"insert_before", "insert_after"}:
            anchor = _normalize_newlines(parameters.anchor)
            content = _normalize_newlines(parameters.content)
            positions = _non_overlapping_positions(text, anchor)
            if len(positions) != parameters.expected_count:
                raise self._occurrence_error(
                    item_id,
                    path,
                    expected=parameters.expected_count,
                    actual=len(positions),
                )
            adjacency = [
                _is_adjacent(
                    text,
                    position,
                    anchor,
                    content,
                    before=action.name == "insert_before",
                )
                for position in positions
            ]
            if all(adjacency):
                updated = text
            elif any(adjacency):
                raise self._error(
                    PlanningErrorCode.INSERTION_STATE_CONFLICT,
                    "insert content is adjacent to only some expected anchors",
                    item_id,
                    path=path.as_posix(),
                )
            elif action.name == "insert_before":
                updated = text.replace(anchor, f"{content}{anchor}")
            else:
                updated = text.replace(anchor, f"{anchor}{content}")
        else:
            deleted = _normalize_newlines(parameters.text)
            actual = text.count(deleted)
            if actual != parameters.expected_count:
                raise self._occurrence_error(
                    item_id,
                    path,
                    expected=parameters.expected_count,
                    actual=actual,
                )
            updated = text.replace(deleted, "")

        disposition = self._update_state(state, updated, item_id=item_id)
        self.action_plans.append(
            PlannedAction(
                id=item_id,
                name=action.name,
                disposition=disposition,
                paths=(path,),
            )
        )

    def _plan_apply_diff(self, action: Action, *, item_id: str) -> None:
        parameters = action.parameters
        file_diffs = parse_unified_diff(
            parameters.diff,
            strip=parameters.strip,
            item_id=item_id,
        )
        paths: list[PurePosixPath] = []
        changed = False
        for file_diff in file_diffs:
            path = self.policy.normalize(file_diff.path)
            target = self.policy.resolve(path, allow_missing=True)
            state = self._get_file(path, target, item_id=item_id)
            if state.original_bytes is None:
                raise self._error(
                    PlanningErrorCode.DIFF_PATH_INVALID,
                    "apply_diff accepts only files that existed before this job",
                    item_id,
                    path=path.as_posix(),
                )
            updated = apply_file_diff(state.text, file_diff, item_id=item_id)
            disposition = self._update_state(state, updated, item_id=item_id)
            changed = changed or disposition is ActionDisposition.MODIFY
            paths.append(path)
        self.action_plans.append(
            PlannedAction(
                id=item_id,
                name=action.name,
                disposition=(
                    ActionDisposition.MODIFY if changed else ActionDisposition.NO_CHANGE
                ),
                paths=tuple(paths),
            )
        )

    def _plan_check(self, check: Check, *, item_id: str) -> None:
        parameters = check.parameters
        paths: tuple[PurePosixPath, ...] = ()
        if check.name == "compileall":
            paths = tuple(
                self._check_target(path, item_id=item_id).relative
                for path in parameters.paths
            )
        elif check.name == "pytest":
            self._validate_pytest_args(parameters.args, item_id=item_id)
            paths = tuple(
                self._check_target(path, item_id=item_id).relative
                for path in parameters.paths
            )
            self._require_module("pytest", item_id=item_id)
        elif check.name == "unittest":
            target = self._check_target(
                parameters.discover,
                item_id=item_id,
                expected=PathKind.DIRECTORY,
            )
            paths = (target.relative,)
        elif check.name in {
            "django_check",
            "django_migrations_check",
            "django_test",
        }:
            target = self._check_target(
                parameters.manage_py,
                item_id=item_id,
                expected=PathKind.FILE,
            )
            paths = (target.relative,)
            if check.name == "django_test":
                invalid_labels = [
                    label
                    for label in parameters.labels
                    if _DJANGO_LABEL.fullmatch(label) is None
                ]
                if invalid_labels:
                    raise self._error(
                        PlanningErrorCode.CHECK_ARGUMENT_INVALID,
                        "Django test labels must be dotted Python identifiers",
                        item_id,
                        path=invalid_labels[0],
                    )
            self._require_module("django", item_id=item_id)
        elif check.name == "profile":
            if parameters.name not in self.workspace.config.checks.profiles:
                raise self._error(
                    PlanningErrorCode.CHECK_PROFILE_NOT_FOUND,
                    "requested check profile is not defined in local configuration",
                    item_id,
                    path=parameters.name,
                )
            self._require_profile_command(parameters.name, item_id=item_id)

        self.check_plans.append(PlannedCheck(id=item_id, name=check.name, paths=paths))

    def _check_target(
        self,
        value: str,
        *,
        item_id: str,
        expected: PathKind | None = None,
    ) -> WorkspacePath:
        path = self.policy.normalize(value)
        if self.policy.is_ignored(path):
            raise self._error(
                PlanningErrorCode.PATH_IGNORED,
                "check target is ignored by local policy",
                item_id,
                path=path.as_posix(),
            )
        target = self.policy.resolve(path, allow_root=True, allow_missing=True)
        kind = self._virtual_kind(path, target, item_id=item_id)
        if kind is PathKind.MISSING:
            raise self._error(
                PlanningErrorCode.CHECK_PATH_NOT_FOUND,
                "check target does not exist in the planned workspace",
                item_id,
                path=path.as_posix(),
            )
        if expected is not None and kind is not expected:
            raise self._error(
                PlanningErrorCode.TARGET_TYPE_INVALID,
                f"expected a {expected.value} check target",
                item_id,
                path=path.as_posix(),
            )
        if target.exists:
            self._record_fingerprint(target, item_id=item_id)
        return WorkspacePath(relative=path, absolute=target.absolute, kind=kind)

    def _validate_pytest_args(self, args: tuple[str, ...], *, item_id: str) -> None:
        for argument in args:
            allowed = argument in _PYTEST_EXACT_ARGS
            if argument.startswith("--maxfail="):
                value = argument.removeprefix("--maxfail=")
                allowed = value.isdigit() and int(value) > 0
            elif argument.startswith("--tb="):
                allowed = argument.removeprefix("--tb=") in _PYTEST_TB_VALUES
            elif argument.startswith("--capture="):
                allowed = argument.removeprefix("--capture=") in _PYTEST_CAPTURE_VALUES
            if not allowed:
                raise self._error(
                    PlanningErrorCode.PYTEST_ARGUMENT_FORBIDDEN,
                    "pytest argument is not in the local safe allowlist",
                    item_id,
                    path=argument,
                )

    def _require_module(self, name: str, *, item_id: str) -> None:
        try:
            available = find_spec(name) is not None
        except (ImportError, ValueError):
            available = False
        if not available:
            raise self._error(
                PlanningErrorCode.DEPENDENCY_NOT_AVAILABLE,
                f"required Python module {name!r} is not available",
                item_id,
                path=name,
            )

    def _require_profile_command(self, name: str, *, item_id: str) -> None:
        profile = self.workspace.config.checks.profiles[name]
        command = profile.argv[0]
        if command == "{python}":
            available = Path(sys.executable).is_file()
        elif Path(command).is_absolute() or "/" in command or "\\" in command:
            candidate = Path(command)
            if not candidate.is_absolute():
                candidate = self.workspace.root / candidate
            available = candidate.is_file()
        else:
            available = shutil.which(command) is not None
        if not available:
            raise self._error(
                PlanningErrorCode.DEPENDENCY_NOT_AVAILABLE,
                "local check profile executable is not available",
                item_id,
                path=command,
            )

    def _virtual_kind(
        self,
        path: PurePosixPath,
        target: WorkspacePath,
        *,
        item_id: str,
    ) -> PathKind:
        self._reject_virtual_file_parent(path, item_id=item_id)
        if path in self.files:
            return PathKind.FILE
        if path in self.created_directories:
            return PathKind.DIRECTORY
        return target.kind

    def _reject_virtual_file_parent(
        self,
        path: PurePosixPath,
        *,
        item_id: str,
    ) -> None:
        for index in range(1, len(path.parts)):
            parent = PurePosixPath(*path.parts[:index])
            if parent in self.files:
                raise self._error(
                    PlanningErrorCode.VIRTUAL_PATH_CONFLICT,
                    "a planned file cannot be used as a parent directory",
                    item_id,
                    path=parent.as_posix(),
                )

    def _ensure_parent_directories(
        self,
        path: PurePosixPath,
        *,
        item_id: str,
    ) -> None:
        for index in range(1, len(path.parts)):
            parent = PurePosixPath(*path.parts[:index])
            if parent in self.files:
                raise self._error(
                    PlanningErrorCode.VIRTUAL_PATH_CONFLICT,
                    "a planned file cannot be used as a parent directory",
                    item_id,
                    path=parent.as_posix(),
                )
            if parent in self.created_directories:
                continue
            target = self.policy.resolve(parent, allow_missing=True)
            if target.kind is PathKind.FILE:
                raise self._error(
                    PlanningErrorCode.TARGET_TYPE_INVALID,
                    "an existing parent is not a directory",
                    item_id,
                    path=parent.as_posix(),
                )
            if target.kind is PathKind.MISSING:
                self.created_directories.setdefault(parent, None)
            else:
                self._record_fingerprint(target, item_id=item_id)

    def _get_file(
        self,
        path: PurePosixPath,
        target: WorkspacePath,
        *,
        item_id: str,
    ) -> _TextState:
        self._reject_virtual_file_parent(path, item_id=item_id)
        if path in self.created_directories:
            raise self._error(
                PlanningErrorCode.TARGET_TYPE_INVALID,
                "text action target is a planned directory",
                item_id,
                path=path.as_posix(),
            )
        existing = self.files.get(path)
        if existing is not None:
            return existing
        if target.kind is not PathKind.FILE:
            raise self._error(
                PlanningErrorCode.TARGET_TYPE_INVALID,
                "text action requires an existing regular file",
                item_id,
                path=path.as_posix(),
            )
        raw = self._read_existing_file(target, item_id=item_id)
        state = self._decode_existing(raw, item_id=item_id, path=path)
        self.files[path] = state
        return state

    def _new_file_state(
        self,
        path: PurePosixPath,
        content: str,
        encoding: str,
        newline: NewlineStyle,
        *,
        item_id: str,
    ) -> _TextState:
        normalized = _normalize_newlines(content)
        if any(_is_binary_control(character) for character in normalized):
            raise self._error(
                PlanningErrorCode.CONTENT_BINARY_FORBIDDEN,
                "create_file content contains binary control characters",
                item_id,
                path=path.as_posix(),
            )
        rendered = _render_newlines(normalized, newline)
        try:
            codec = codecs.lookup(encoding).name
            raw = rendered.encode(encoding)
        except (LookupError, UnicodeError, TypeError) as exc:
            raise self._error(
                PlanningErrorCode.CONTENT_ENCODING_INVALID,
                "content cannot be encoded with the requested text encoding",
                item_id,
                path=path.as_posix(),
            ) from exc
        self._require_size(raw, item_id=item_id, path=path)
        return _TextState(
            path=path,
            original_bytes=None,
            current_bytes=raw,
            text=normalized,
            encoding=codec,
            codec=encoding,
            bom=b"",
            newline=newline,
        )

    def _decode_existing(
        self,
        raw: bytes,
        *,
        item_id: str,
        path: PurePosixPath,
    ) -> _TextState:
        bom = b""
        if raw.startswith(_UTF32_LE_BOM):
            bom, codec, encoding = _UTF32_LE_BOM, "utf-32-le", "utf-32-le"
        elif raw.startswith(_UTF32_BE_BOM):
            bom, codec, encoding = _UTF32_BE_BOM, "utf-32-be", "utf-32-be"
        elif raw.startswith(_UTF8_BOM):
            bom, codec, encoding = _UTF8_BOM, "utf-8", "utf-8-sig"
        elif raw.startswith(_UTF16_LE_BOM):
            bom, codec, encoding = _UTF16_LE_BOM, "utf-16-le", "utf-16-le"
        elif raw.startswith(_UTF16_BE_BOM):
            bom, codec, encoding = _UTF16_BE_BOM, "utf-16-be", "utf-16-be"
        else:
            codec = encoding = "utf-8"
            if b"\0" in raw:
                raise self._error(
                    PlanningErrorCode.FILE_BINARY,
                    "file contains null bytes",
                    item_id,
                    path=path.as_posix(),
                )

        try:
            text = raw[len(bom) :].decode(codec)
        except UnicodeDecodeError as exc:
            raise self._error(
                PlanningErrorCode.FILE_ENCODING_UNSUPPORTED,
                "file is not valid UTF text in a supported encoding",
                item_id,
                path=path.as_posix(),
            ) from exc
        if any(_is_binary_control(character) for character in text):
            raise self._error(
                PlanningErrorCode.FILE_BINARY,
                "file contains binary control characters",
                item_id,
                path=path.as_posix(),
            )

        newline, normalized = self._detect_newlines(
            text,
            item_id=item_id,
            path=path,
        )
        return _TextState(
            path=path,
            original_bytes=raw,
            current_bytes=raw,
            text=normalized,
            encoding=encoding,
            codec=codec,
            bom=bom,
            newline=newline,
        )

    def _detect_newlines(
        self,
        text: str,
        *,
        item_id: str,
        path: PurePosixPath,
    ) -> tuple[NewlineStyle, str]:
        without_crlf = text.replace("\r\n", "")
        has_crlf = "\r\n" in text
        has_lf = "\n" in without_crlf
        if "\r" in without_crlf or (has_crlf and has_lf):
            raise self._error(
                PlanningErrorCode.FILE_NEWLINE_UNSUPPORTED,
                "mixed or CR-only newlines are not supported for text changes",
                item_id,
                path=path.as_posix(),
            )
        if has_crlf:
            return NewlineStyle.CRLF, text.replace("\r\n", "\n")
        if has_lf:
            return NewlineStyle.LF, text
        return NewlineStyle.NONE, text

    def _update_state(
        self,
        state: _TextState,
        updated: str,
        *,
        item_id: str,
    ) -> ActionDisposition:
        newline = state.newline
        if newline is NewlineStyle.NONE and "\n" in updated:
            newline = NewlineStyle.LF
        rendered = _render_newlines(updated, newline)
        try:
            raw = state.bom + rendered.encode(state.codec)
        except (LookupError, UnicodeError) as exc:
            raise self._error(
                PlanningErrorCode.CONTENT_ENCODING_INVALID,
                "planned content cannot be represented in the target encoding",
                item_id,
                path=state.path.as_posix(),
            ) from exc
        self._require_size(raw, item_id=item_id, path=state.path)
        if raw == state.current_bytes:
            return ActionDisposition.NO_CHANGE
        state.text = updated
        state.current_bytes = raw
        state.newline = newline
        return ActionDisposition.MODIFY

    def _read_existing_file(
        self,
        target: WorkspacePath,
        *,
        item_id: str,
    ) -> bytes:
        try:
            size = target.absolute.lstat().st_size
        except OSError as exc:
            raise self._error(
                PlanningErrorCode.FILE_READ_FAILED,
                "file metadata could not be read",
                item_id,
                path=target.relative.as_posix(),
            ) from exc
        if size > self.max_file_bytes:
            raise self._error(
                PlanningErrorCode.FILE_SIZE_LIMIT_EXCEEDED,
                f"file exceeds the configured {self.max_file_bytes}-byte limit",
                item_id,
                path=target.relative.as_posix(),
            )
        try:
            raw = target.absolute.read_bytes()
        except OSError as exc:
            raise self._error(
                PlanningErrorCode.FILE_READ_FAILED,
                "file could not be read",
                item_id,
                path=target.relative.as_posix(),
            ) from exc
        self._require_size(raw, item_id=item_id, path=target.relative)
        revalidated = self.policy.resolve(target.relative)
        if revalidated.absolute != target.absolute:
            raise self._error(
                PlanningErrorCode.FILE_READ_FAILED,
                "file path changed during planning",
                item_id,
                path=target.relative.as_posix(),
            )
        self._record_fingerprint(target, item_id=item_id, raw=raw)
        return raw

    def _record_fingerprint(
        self,
        target: WorkspacePath,
        *,
        item_id: str,
        raw: bytes | None = None,
    ) -> None:
        if not target.exists:
            return
        try:
            metadata = target.absolute.lstat()
        except OSError as exc:
            raise self._error(
                PlanningErrorCode.FILE_READ_FAILED,
                "target metadata could not be retained for revalidation",
                item_id,
                path=target.relative.as_posix(),
            ) from exc
        fingerprint = PathFingerprint(
            path=target.relative,
            kind=target.kind,
            size=metadata.st_size,
            modified_ns=metadata.st_mtime_ns,
            sha256=hashlib.sha256(raw).hexdigest() if raw is not None else None,
        )
        previous = self.fingerprints.get(target.relative)
        if previous is None or fingerprint.sha256 is not None:
            self.fingerprints[target.relative] = fingerprint

    def _require_size(
        self,
        raw: bytes,
        *,
        item_id: str,
        path: PurePosixPath,
    ) -> None:
        if len(raw) > self.max_file_bytes:
            raise self._error(
                PlanningErrorCode.FILE_SIZE_LIMIT_EXCEEDED,
                f"planned file exceeds the configured {self.max_file_bytes}-byte limit",
                item_id,
                path=path.as_posix(),
            )

    def _occurrence_error(
        self,
        item_id: str,
        path: PurePosixPath,
        *,
        expected: int,
        actual: int,
    ) -> PlanningError:
        return self._error(
            PlanningErrorCode.OCCURRENCE_COUNT_MISMATCH,
            f"expected {expected} exact occurrence(s), found {actual}",
            item_id,
            path=path.as_posix(),
        )

    def _build_file_changes(self) -> tuple[PlannedFileChange, ...]:
        changes: list[PlannedFileChange] = []
        for state in self.files.values():
            if (
                state.original_bytes is not None
                and state.current_bytes == state.original_bytes
            ):
                continue
            disposition = (
                FileDisposition.CREATE
                if state.original_bytes is None
                else FileDisposition.MODIFY
            )
            changes.append(
                PlannedFileChange(
                    path=state.path,
                    disposition=disposition,
                    before_sha256=(
                        hashlib.sha256(state.original_bytes).hexdigest()
                        if state.original_bytes is not None
                        else None
                    ),
                    after_sha256=hashlib.sha256(state.current_bytes).hexdigest(),
                    before_size=(
                        len(state.original_bytes)
                        if state.original_bytes is not None
                        else None
                    ),
                    after_size=len(state.current_bytes),
                    encoding=state.encoding,
                    newline=state.newline,
                    content=state.current_bytes,
                )
            )
        return tuple(changes)

    def _formatting_targets(
        self,
        changes: tuple[PlannedFileChange, ...],
    ) -> tuple[PurePosixPath, ...]:
        if not self.workspace.config.formatting.enabled:
            return ()
        return tuple(change.path for change in changes if change.path.suffix == ".py")

    @staticmethod
    def _error(
        code: PlanningErrorCode,
        message: str,
        item_id: str,
        *,
        path: str | None = None,
    ) -> PlanningError:
        return PlanningError(code, message, item_id=item_id, path=path)


def normalized_job_hash(job: Job) -> str:
    """Return the stable protocol hash used by plans and the registry."""

    normalized = json.dumps(
        job.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(normalized).hexdigest()


def _normalize_newlines(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _render_newlines(value: str, newline: NewlineStyle) -> str:
    return value.replace("\n", "\r\n") if newline is NewlineStyle.CRLF else value


def _is_binary_control(character: str) -> bool:
    value = ord(character)
    return (value < 32 and character not in "\t\n\r") or value == 127


def _non_overlapping_positions(text: str, value: str) -> tuple[int, ...]:
    positions: list[int] = []
    start = 0
    while True:
        position = text.find(value, start)
        if position < 0:
            return tuple(positions)
        positions.append(position)
        start = position + len(value)


def _is_adjacent(
    text: str,
    position: int,
    anchor: str,
    content: str,
    *,
    before: bool,
) -> bool:
    if before:
        start = position - len(content)
        return start >= 0 and text[start:position] == content
    start = position + len(anchor)
    return text[start : start + len(content)] == content


__all__ = [
    "ActionDisposition",
    "FileDisposition",
    "NewlineStyle",
    "PathFingerprint",
    "Plan",
    "PlannedAction",
    "PlannedCheck",
    "PlannedFileChange",
    "normalized_job_hash",
    "plan_job",
]
