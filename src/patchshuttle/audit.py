"""Bounded read-only audit action execution."""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import stat
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path, PurePosixPath

from patchshuttle._process import ProcessCommand, ProcessStatus, run_process
from patchshuttle._version import __version__
from patchshuttle.errors import ExecutionError, ExecutionErrorCode, PolicyError
from patchshuttle.inventory import (
    InventoryError,
    WorkspaceComparison,
    capture_inventory,
    compare_inventories,
)
from patchshuttle.models import JobKind
from patchshuttle.planner import Plan, plan_job
from patchshuttle.policy import PathKind, Policy, WorkspacePath

_UTF32_LE_BOM = b"\xff\xfe\x00\x00"
_UTF32_BE_BOM = b"\x00\x00\xfe\xff"
_UTF16_LE_BOM = b"\xff\xfe"
_UTF16_BE_BOM = b"\xfe\xff"
_UTF8_BOM = b"\xef\xbb\xbf"
_TRUNCATION_MARKER = "\n[TRUNCATED BY PATCHSHUTTLE]\n"


class AuditStatus(str):
    """Stable audit action outcomes."""

    COMPLETED = "COMPLETED"
    NOT_AVAILABLE = "NOT_AVAILABLE"


@dataclass(frozen=True, slots=True)
class AuditActionResult:
    """One bounded audit action observation."""

    id: str
    name: str
    status: str
    scope: tuple[PurePosixPath, ...]
    started_at: str
    duration_ms: int
    output: str = field(repr=False)
    output_truncated: bool = False

    @property
    def success(self) -> bool:
        return self.status in {AuditStatus.COMPLETED, AuditStatus.NOT_AVAILABLE}


@dataclass(frozen=True, slots=True)
class AuditRunResult:
    """Ordered results from a read-only audit plan."""

    plan: Plan = field(repr=False)
    results: tuple[AuditActionResult, ...]
    workspace_comparison: WorkspaceComparison


@dataclass(frozen=True, slots=True)
class _WalkEntry:
    path: PurePosixPath
    absolute: Path
    mode: int
    depth: int


def execute_audit_locked(plan: Plan) -> AuditRunResult:
    """Execute one audit while the caller holds the workspace run lock."""

    if plan.job.kind is not JobKind.AUDIT:
        raise ExecutionError(
            ExecutionErrorCode.JOB_KIND_UNSUPPORTED,
            "the audit runner accepts only audit jobs",
        )
    _revalidate_plan(plan)
    baseline = _capture_inventory(plan)
    results: list[AuditActionResult] = []
    for action, planned in zip(plan.job.actions, plan.actions):
        started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        started = time.monotonic_ns()
        try:
            status, output = _execute_action(plan, action.name, action.parameters)
            output, truncated = _bounded_output(
                output,
                plan.workspace.config.execution.max_command_output_bytes,
            )
            truncated = truncated or _TRUNCATION_MARKER.strip() in output
        except ExecutionError as error:
            error.item_id = error.item_id or planned.id
            error.path = error.path or (
                planned.paths[0].as_posix() if planned.paths else None
            )
            error.audit_results = tuple(results)
            raise
        except (OSError, PolicyError, UnicodeError, ValueError) as exc:
            raise ExecutionError(
                ExecutionErrorCode.ACTION_FAILED,
                "a read-only audit action failed",
                item_id=planned.id,
                path=(planned.paths[0].as_posix() if planned.paths else None),
                audit_results=tuple(results),
            ) from exc
        results.append(
            AuditActionResult(
                id=planned.id,
                name=planned.name,
                status=status,
                scope=planned.paths,
                started_at=started_at,
                duration_ms=(time.monotonic_ns() - started) // 1_000_000,
                output=output,
                output_truncated=truncated,
            )
        )
    current = _capture_inventory(plan)
    comparison = compare_inventories(baseline, current)
    if comparison.unexpected_changes:
        first = comparison.unexpected_changes[0]
        raise ExecutionError(
            ExecutionErrorCode.UNEXPECTED_WORKSPACE_CHANGE,
            "an audit action changed the workspace",
            path=first.path.as_posix(),
            audit_results=tuple(results),
            workspace_comparison=comparison,
        )
    return AuditRunResult(
        plan=plan,
        results=tuple(results),
        workspace_comparison=comparison,
    )


def _execute_action(plan: Plan, name: str, parameters) -> tuple[str, str]:
    if name == "tree":
        return AuditStatus.COMPLETED, _tree(plan, parameters)
    if name == "read":
        return AuditStatus.COMPLETED, _read(plan, parameters)
    if name == "search":
        return AuditStatus.COMPLETED, _search(plan, parameters)
    if name == "find_files":
        return AuditStatus.COMPLETED, _find_files(plan, parameters)
    if name == "file_info":
        return AuditStatus.COMPLETED, _file_info(plan, parameters)
    if name == "hash":
        return AuditStatus.COMPLETED, _hash(plan, parameters)
    if name == "git_status":
        return _git_status(plan)
    if name == "environment":
        return AuditStatus.COMPLETED, _environment(plan)
    raise ExecutionError(
        ExecutionErrorCode.ACTION_UNSUPPORTED,
        "the audit runner does not support this action",
    )


def _tree(plan: Plan, parameters) -> str:
    policy = Policy(plan.workspace)
    root = policy.resolve(parameters.path, allow_root=True)
    lines = [f"{_display(root.relative)}/ [directory]"]
    entries = _walk(
        plan,
        root,
        maximum_depth=parameters.depth,
        include_hidden=parameters.include_hidden,
    )
    limited = False
    for entry in entries:
        if len(lines) - 1 >= parameters.max_entries:
            limited = True
            break
        kind = _mode_name(entry.mode)
        suffix = "/" if stat.S_ISDIR(entry.mode) else ""
        lines.append(f"{entry.path.as_posix()}{suffix} [{kind}]")
    if limited:
        lines.append("[ENTRY LIMIT REACHED]")
    return "\n".join(lines)


def _read(plan: Plan, parameters) -> str:
    policy = Policy(plan.workspace)
    target = policy.resolve(parameters.path)
    raw = _read_regular_file(plan, target)
    encoding, text = _decode_text(raw)
    lines = text.splitlines()
    start = parameters.start_line
    end = parameters.end_line or len(lines)
    selected = [
        f"{number:>6}: {lines[number - 1]}"
        for number in range(start, min(end, len(lines)) + 1)
    ]
    header = f"path: {target.relative.as_posix()}\nencoding: {encoding}"
    output = header + ("\n" + "\n".join(selected) if selected else "\n[NO LINES]")
    limit = (
        parameters.max_bytes or plan.workspace.config.execution.max_single_file_bytes
    )
    bounded, _ = _bounded_output(output, limit)
    return bounded


def _search(plan: Plan, parameters) -> str:
    policy = Policy(plan.workspace)
    root = policy.resolve(parameters.path, allow_root=True)
    files = _audit_files(plan, root, glob=parameters.glob)
    needle = (
        parameters.text if parameters.case_sensitive else parameters.text.casefold()
    )
    results: list[str] = []
    skipped_binary = 0
    for path, target in files:
        try:
            _, text = _decode_text(_read_regular_file(plan, target))
        except (UnicodeError, ValueError):
            skipped_binary += 1
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            compared = line if parameters.case_sensitive else line.casefold()
            if needle in compared:
                results.append(f"{path.as_posix()}:{number}:{line}")
                if len(results) >= parameters.max_results:
                    break
        if len(results) >= parameters.max_results:
            break
    header = [
        f"literal: {parameters.text}",
        f"case_sensitive: {str(parameters.case_sensitive).lower()}",
        f"matches: {len(results)}",
        f"binary_files_skipped: {skipped_binary}",
    ]
    if len(results) >= parameters.max_results:
        header.append("result_limit_reached: true")
    return "\n".join((*header, *results))


def _find_files(plan: Plan, parameters) -> str:
    policy = Policy(plan.workspace)
    root = policy.resolve(parameters.path, allow_root=True)
    found: list[str] = []
    for path, _ in _audit_files(plan, root, glob=parameters.glob):
        found.append(path.as_posix())
        if len(found) >= parameters.max_results:
            break
    lines = [f"glob: {parameters.glob}", f"matches: {len(found)}", *found]
    if len(found) >= parameters.max_results:
        lines.insert(2, "result_limit_reached: true")
    return "\n".join(lines)


def _file_info(plan: Plan, parameters) -> str:
    policy = Policy(plan.workspace)
    target = policy.resolve(parameters.path)
    metadata_value = target.absolute.lstat()
    values = [
        f"path: {target.relative.as_posix()}",
        f"type: {target.kind.value}",
        f"size_bytes: {metadata_value.st_size if target.kind is PathKind.FILE else 0}",
        f"executable: {str(bool(metadata_value.st_mode & 0o111)).lower()}",
        "modified_at: "
        + datetime.fromtimestamp(
            metadata_value.st_mtime,
            tz=timezone.utc,
        ).isoformat(timespec="seconds"),
    ]
    if target.kind is PathKind.FILE:
        raw = _read_regular_file(plan, target)
        try:
            encoding, text = _decode_text(raw)
            newline = _newline_style(text)
        except (UnicodeError, ValueError):
            encoding, newline = "binary_or_unsupported", "not_applicable"
        values.extend((f"encoding: {encoding}", f"newline: {newline}"))
    return "\n".join(values)


def _hash(plan: Plan, parameters) -> str:
    target = Policy(plan.workspace).resolve(parameters.path)
    raw = _read_regular_file(plan, target)
    return "\n".join(
        (
            f"path: {target.relative.as_posix()}",
            "algorithm: sha256",
            f"sha256: {hashlib.sha256(raw).hexdigest()}",
            f"size_bytes: {len(raw)}",
        )
    )


def _git_status(plan: Plan) -> tuple[str, str]:
    git = shutil.which("git")
    marker = plan.workspace.root / ".git"
    if git is None or not marker.exists() or marker.is_symlink():
        return AuditStatus.NOT_AVAILABLE, "Git repository or executable not available"
    process = run_process(
        ProcessCommand(
            argv=(
                git,
                "-c",
                "color.ui=false",
                "status",
                "--short",
                "--branch",
                "--untracked-files=normal",
            ),
            working_directory=plan.workspace.root,
            timeout_seconds=plan.workspace.config.execution.default_timeout_seconds,
        ),
        maximum_output_bytes=plan.workspace.config.execution.max_command_output_bytes,
    )
    if process.status is not ProcessStatus.PASSED:
        raise OSError(process.stderr or "git status failed")
    output = process.stdout or "[CLEAN WORKTREE]"
    if process.stdout_truncated:
        output += _TRUNCATION_MARKER
    return AuditStatus.COMPLETED, output.rstrip("\n")


def _environment(plan: Plan) -> str:
    values = {
        "operating_system": platform.platform(),
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "patchshuttle_version": __version__,
        "project_id": plan.workspace.project_id,
        "working_directory": _redacted_cwd(plan.workspace.root),
        "git": _tool_version("git"),
        "pytest": _package_version("pytest"),
        "isort": _package_version("isort"),
        "black": _package_version("black"),
    }
    return "\n".join(f"{key}: {value}" for key, value in values.items())


def _walk(
    plan: Plan,
    root: WorkspacePath,
    *,
    maximum_depth: int,
    include_hidden: bool,
) -> tuple[_WalkEntry, ...]:
    policy = Policy(plan.workspace)
    pending = [(root.absolute, root.relative, 0)]
    result: list[_WalkEntry] = []
    inspected = 0
    maximum = plan.workspace.config.execution.max_inventory_entries
    while pending:
        directory, parent, depth = pending.pop()
        try:
            with os.scandir(directory) as iterator:
                children = sorted(iterator, key=lambda item: item.name)
        except OSError as exc:
            raise OSError(f"could not inspect {parent.as_posix()}") from exc
        directories: list[tuple[Path, PurePosixPath, int]] = []
        for child in children:
            relative = parent / child.name
            if (not include_hidden and child.name.startswith(".")) or _skip(
                policy,
                relative,
            ):
                continue
            inspected += 1
            if inspected > maximum:
                raise OSError("audit traversal exceeded the configured entry limit")
            try:
                metadata_value = child.stat(follow_symlinks=False)
            except OSError as exc:
                raise OSError(f"could not inspect {relative.as_posix()}") from exc
            result.append(
                _WalkEntry(
                    path=relative,
                    absolute=Path(child.path),
                    mode=metadata_value.st_mode,
                    depth=depth + 1,
                )
            )
            if stat.S_ISDIR(metadata_value.st_mode) and depth + 1 < maximum_depth:
                directories.append((Path(child.path), relative, depth + 1))
        pending.extend(reversed(directories))
    return tuple(result)


def _audit_files(
    plan: Plan,
    root: WorkspacePath,
    *,
    glob: str | None,
) -> tuple[tuple[PurePosixPath, WorkspacePath], ...]:
    policy = Policy(plan.workspace)
    if root.kind is PathKind.FILE:
        candidates = (root.relative,)
        base = root.relative.parent
    else:
        entries = _walk(
            plan,
            root,
            maximum_depth=10_000,
            include_hidden=True,
        )
        candidates = tuple(entry.path for entry in entries if stat.S_ISREG(entry.mode))
        base = root.relative
    files: list[tuple[PurePosixPath, WorkspacePath]] = []
    for path in candidates:
        compared = path.relative_to(base) if base.parts else path
        if glob is not None and not (
            compared.match(glob) or PurePosixPath(path.name).match(glob)
        ):
            continue
        target = policy.resolve(path)
        if target.kind is PathKind.FILE:
            files.append((path, target))
    return tuple(files)


def _read_regular_file(plan: Plan, target: WorkspacePath) -> bytes:
    if target.kind is not PathKind.FILE:
        raise OSError("audit target is not a regular file")
    before = target.absolute.lstat()
    maximum = plan.workspace.config.execution.max_single_file_bytes
    if before.st_size > maximum:
        raise OSError("audit file exceeds the configured size limit")
    raw = target.absolute.read_bytes()
    after = target.absolute.lstat()
    current = Policy(plan.workspace).resolve(target.relative)
    identity = lambda item: (
        item.st_dev,
        item.st_ino,
        item.st_size,
        item.st_mtime_ns,
        item.st_mode,
    )
    if (
        len(raw) > maximum
        or identity(before) != identity(after)
        or current.kind is not PathKind.FILE
        or current.absolute != target.absolute
    ):
        raise OSError("audit file changed while it was read")
    return raw


def _decode_text(raw: bytes) -> tuple[str, str]:
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
            raise ValueError("binary file")
    text = raw[len(bom) :].decode(codec)
    if any(_is_binary_control(character) for character in text):
        raise ValueError("binary file")
    return encoding, text


def _newline_style(text: str) -> str:
    without_crlf = text.replace("\r\n", "")
    has_crlf = "\r\n" in text
    has_lf = "\n" in without_crlf
    if "\r" in without_crlf or (has_crlf and has_lf):
        return "mixed_or_cr"
    if has_crlf:
        return "crlf"
    if has_lf:
        return "lf"
    return "none"


def _bounded_output(value: str, maximum: int) -> tuple[str, bool]:
    raw = value.encode("utf-8")
    if len(raw) <= maximum:
        return value, False
    marker = _TRUNCATION_MARKER.encode("utf-8")
    if maximum <= len(marker):
        return marker[:maximum].decode("utf-8"), True
    retained = raw[: max(0, maximum - len(marker))]
    return retained.decode("utf-8", errors="ignore") + _TRUNCATION_MARKER, True


def _skip(policy: Policy, path: PurePosixPath) -> bool:
    return policy.is_ignored(path) or policy.is_protected(path)


def _mode_name(mode: int) -> str:
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISLNK(mode):
        return "symlink"
    return "other"


def _display(path: PurePosixPath) -> str:
    return path.as_posix() if path.parts else "."


def _redacted_cwd(path: Path) -> str:
    try:
        home = Path.home().resolve()
        resolved = path.resolve()
        if resolved == home:
            return "~"
        return "~/" + resolved.relative_to(home).as_posix()
    except (OSError, ValueError):
        return path.as_posix()


def _package_version(name: str) -> str:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return "NOT_AVAILABLE"


def _tool_version(name: str) -> str:
    executable = shutil.which(name)
    if executable is None:
        return "NOT_AVAILABLE"
    process = run_process(
        ProcessCommand(
            argv=(executable, "--version"),
            working_directory=Path.cwd(),
            timeout_seconds=10,
        ),
        maximum_output_bytes=4096,
    )
    if process.status is not ProcessStatus.PASSED:
        return "NOT_AVAILABLE"
    return (process.stdout or process.stderr).strip() or "AVAILABLE"


def _is_binary_control(character: str) -> bool:
    value = ord(character)
    return (value < 32 and character not in "\t\n\r") or value == 127


def _revalidate_plan(plan: Plan) -> None:
    try:
        current = plan_job(plan.job, plan.workspace)
    except (OSError, PolicyError, ValueError) as exc:
        raise ExecutionError(
            ExecutionErrorCode.PLAN_STALE,
            "the workspace no longer matches the approved audit plan",
            item_id=getattr(exc, "item_id", None),
            path=getattr(exc, "path", None),
        ) from exc
    if current != plan:
        raise ExecutionError(
            ExecutionErrorCode.PLAN_STALE,
            "the workspace no longer matches the approved audit plan",
        )


def _capture_inventory(plan: Plan):
    try:
        return capture_inventory(plan.workspace)
    except InventoryError as exc:
        raise ExecutionError(
            ExecutionErrorCode.WORKSPACE_INVENTORY_FAILED,
            "audit workspace inventory could not be captured",
            path=exc.path.as_posix() if exc.path is not None else None,
        ) from exc


__all__ = [
    "AuditActionResult",
    "AuditRunResult",
    "AuditStatus",
    "execute_audit_locked",
]
