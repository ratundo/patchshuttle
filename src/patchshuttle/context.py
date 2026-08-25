"""Bounded project snapshots and upload-friendly AI handoffs."""

from __future__ import annotations

import json
import platform
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

from patchshuttle._process import ProcessCommand, ProcessStatus, run_process
from patchshuttle._version import __version__
from patchshuttle.errors import ExecutionError, ExecutionErrorCode
from patchshuttle.inventory import (
    InventoryEntryKind,
    InventoryError,
    WorkspaceInventory,
    capture_inventory,
)
from patchshuttle.logging import capabilities_hash, current_run_clock, write_named_log
from patchshuttle.registry import Registry, load_registry
from patchshuttle.runner import acquire_workspace_lock
from patchshuttle.selfdoc import AUDIT_ACTIONS, CHANGE_ACTIONS, CHECKS
from patchshuttle.workspace import Workspace

_AUDIT_ACTIONS = ", ".join(AUDIT_ACTIONS)
_CHANGE_ACTIONS = ", ".join(CHANGE_ACTIONS)
_CHECKS = ", ".join(CHECKS)
_TREE_LIMIT = 500
_FILE_LIMIT = 2_000
_HISTORY_LIMIT = 20
_HANDOFF_NOISE_SUFFIXES = (".tar.gz",)
_TRUNCATION = "\n[TRUNCATED BY PATCHSHUTTLE]\n"


@dataclass(frozen=True, slots=True)
class SnapshotResult:
    """One generated read-only project snapshot."""

    path: Path
    inventory_entries: int
    output_truncated: bool


@dataclass(frozen=True, slots=True)
class HandoffResult:
    """One generated AI-facing project handoff."""

    path: Path
    inventory_entries: int
    recent_jobs: int
    output_truncated: bool


def create_snapshot(workspace: Workspace) -> SnapshotResult:
    """Capture bounded metadata without including source-file contents."""

    clock = current_run_clock(workspace)
    with acquire_workspace_lock(workspace):
        inventory = _capture(workspace)
        registry = load_registry(workspace)
        content = _snapshot_text(workspace, inventory, registry, clock.iso_timestamp)
        content, truncated = _bounded(
            content,
            workspace.config.execution.max_command_output_bytes,
        )
        path = write_named_log(
            workspace,
            clock=clock,
            label="SNAPSHOT",
            content=content,
        )
    return SnapshotResult(
        path=path,
        inventory_entries=len(inventory.entries),
        output_truncated=truncated,
    )


def create_handoff(workspace: Workspace) -> HandoffResult:
    """Create one compact provider-neutral context file for an AI service."""

    clock = current_run_clock(workspace)
    with acquire_workspace_lock(workspace):
        inventory = _capture(workspace)
        registry = load_registry(workspace)
        latest_summary, latest_handoff = _latest_run_context(workspace)
        recent = _recent_records(registry)
        content = _handoff_text(
            workspace,
            inventory,
            recent,
            clock.iso_timestamp,
            latest_summary=latest_summary,
            latest_handoff=latest_handoff,
        )
        content, truncated = _bounded(
            content,
            workspace.config.execution.max_command_output_bytes,
        )
        path = write_named_log(
            workspace,
            clock=clock,
            label="HANDOFF",
            content=content,
        )
    return HandoffResult(
        path=path,
        inventory_entries=len(inventory.entries),
        recent_jobs=len(recent),
        output_truncated=truncated,
    )


def _snapshot_text(
    workspace: Workspace,
    inventory: WorkspaceInventory,
    registry: Registry,
    timestamp: str,
) -> str:
    tree_lines, tree_truncated = _tree_lines(inventory)
    file_lines, files_truncated = _file_lines(inventory)
    recent = _recent_records(registry)
    return "\n".join(
        (
            "=== PATCHSHUTTLE_SNAPSHOT ===",
            "protocol: 1",
            f"timestamp: {timestamp}",
            f"project_id: {workspace.project_id}",
            f"patchshuttle_version: {__version__}",
            f"python_version: {platform.python_version()}",
            f"workspace_root: {workspace.root.as_posix()}",
            f"inventory_entries: {len(inventory.entries)}",
            f"inventory_hashed_bytes: {inventory.hashed_bytes}",
            "",
            "=== CAPABILITIES ===",
            "job_kinds: audit, patch, verify",
            f"audit_actions: {_AUDIT_ACTIONS}",
            f"change_actions: {_CHANGE_ACTIONS}",
            f"checks: {_CHECKS}",
            "",
            "=== POLICY_SUMMARY ===",
            "ignored_paths: "
            + json.dumps(
                workspace.config.project.ignored_paths,
                ensure_ascii=False,
            ),
            "protected_paths: "
            + json.dumps(
                workspace.config.project.protected_paths,
                ensure_ascii=False,
            ),
            "",
            "=== PROJECT_TREE ===",
            *tree_lines,
            f"tree_truncated: {str(tree_truncated).lower()}",
            "",
            "=== FILE_FINGERPRINTS ===",
            *file_lines,
            f"file_list_truncated: {str(files_truncated).lower()}",
            "",
            "=== GIT_STATUS ===",
            _git_status(workspace),
            "",
            "=== RECENT_JOBS ===",
            *(_record_line(item) for item in recent),
            "=== END_PATCHSHUTTLE_SNAPSHOT ===",
        )
    )


def _handoff_text(
    workspace: Workspace,
    inventory: WorkspaceInventory,
    recent: tuple,
    timestamp: str,
    *,
    latest_summary: str,
    latest_handoff: str,
) -> str:
    tree_lines, tree_truncated = _tree_lines(inventory, handoff=True)
    return "\n".join(
        (
            "=== PATCHSHUTTLE_HANDOFF ===",
            "AI_INSTRUCTION:",
            "Inspect this context and the latest run result. Return exactly one "
            ".psh.yaml job using protocol 1 and the project_id below. Use an "
            "audit job when more evidence is needed. Do not return shell commands "
            "or ask PatchShuttle to weaken local policy. After the user runs the "
            "job, request the resulting PatchShuttle log before preparing another job.",
            "",
            "=== PROJECT ===",
            "protocol: 1",
            f"timestamp: {timestamp}",
            f"project_id: {workspace.project_id}",
            f"patchshuttle_version: {__version__}",
            "",
            "=== CAPABILITIES ===",
            "ai_handoff_version: 2",
            f"capabilities_hash: {capabilities_hash()}",
            "capabilities_command: patchshuttle capabilities",
            "",
            "=== LATEST_RUN_SUMMARY ===",
            latest_summary,
            "",
            "=== LATEST_AI_HANDOFF ===",
            latest_handoff,
            "",
            "=== BOUNDED_PROJECT_TREE ===",
            *tree_lines,
            f"tree_truncated: {str(tree_truncated).lower()}",
            "",
            "=== RECENT_JOB_HISTORY ===",
            *(_record_line(item) for item in recent),
            "",
            "EXPECTED_RESPONSE: one .psh.yaml file only",
            "=== END_PATCHSHUTTLE_HANDOFF ===",
        )
    )


def _capture(workspace: Workspace) -> WorkspaceInventory:
    try:
        return capture_inventory(workspace)
    except InventoryError as exc:
        raise ExecutionError(
            ExecutionErrorCode.WORKSPACE_INVENTORY_FAILED,
            "project context inventory could not be captured",
            path=exc.path.as_posix() if exc.path is not None else None,
        ) from exc


def _tree_lines(
    inventory: WorkspaceInventory,
    *,
    handoff: bool = False,
) -> tuple[tuple[str, ...], bool]:
    entries = (
        tuple(entry for entry in inventory.entries if not _is_handoff_tree_noise(entry))
        if handoff
        else inventory.entries
    )
    selected = entries[:_TREE_LIMIT]
    lines = tuple(
        f"{entry.path.as_posix()}{'/' if entry.kind is InventoryEntryKind.DIRECTORY else ''} [{entry.kind.value.lower()}]"
        for entry in selected
    )
    return lines or ("[EMPTY PROJECT]",), len(entries) > len(selected)


def _is_handoff_tree_noise(entry) -> bool:
    if entry.kind is not InventoryEntryKind.FILE:
        return False
    name = entry.path.name.casefold()
    return ".bak_" in name or name.endswith(_HANDOFF_NOISE_SUFFIXES)


def _file_lines(
    inventory: WorkspaceInventory,
) -> tuple[tuple[str, ...], bool]:
    files = tuple(
        entry for entry in inventory.entries if entry.kind is InventoryEntryKind.FILE
    )
    selected = files[:_FILE_LIMIT]
    lines = tuple(
        f"{entry.path.as_posix()} size={entry.size} sha256={entry.sha256}"
        for entry in selected
    )
    return lines or ("[NO FILES]",), len(files) > len(selected)


def _recent_records(registry: Registry) -> tuple:
    return tuple(
        sorted(
            registry.jobs.values(),
            key=lambda item: (item.latest_run_at, item.job_id),
            reverse=True,
        )[:_HISTORY_LIMIT]
    )


def _record_line(record) -> str:
    return (
        f"{record.job_id} kind={record.kind} result={record.latest_result} "
        f"hash={record.job_hash[:8]} at={record.latest_run_at}"
    )


def _git_status(workspace: Workspace) -> str:
    executable = shutil.which("git")
    marker = workspace.root / ".git"
    if executable is None or not marker.exists() or marker.is_symlink():
        return "NOT_AVAILABLE"
    process = run_process(
        ProcessCommand(
            argv=(
                executable,
                "-c",
                "color.ui=false",
                "status",
                "--short",
                "--branch",
                "--untracked-files=normal",
            ),
            working_directory=workspace.root,
            timeout_seconds=workspace.config.execution.default_timeout_seconds,
        ),
        maximum_output_bytes=workspace.config.execution.max_command_output_bytes,
    )
    if process.status is not ProcessStatus.PASSED:
        return "NOT_AVAILABLE"
    value = process.stdout.rstrip("\n") or "CLEAN"
    return value + ("\n[TRUNCATED]" if process.stdout_truncated else "")


def _latest_run_context(workspace: Workspace) -> tuple[str, str]:
    directory = workspace.patches_dir / "logs"
    try:
        candidates = sorted(
            (
                path
                for path in directory.iterdir()
                if path.name.startswith("log_") and path.suffix == ".log"
            ),
            key=lambda path: (path.lstat().st_mtime_ns, path.name),
            reverse=True,
        )
    except OSError as exc:
        raise ExecutionError(
            ExecutionErrorCode.OPERATIONAL_RECORD_FAILED,
            "run logs could not be inspected for handoff generation",
            path="patches/logs",
        ) from exc
    maximum = workspace.config.execution.max_command_output_bytes
    for path in candidates:
        try:
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum:
                continue
            text = path.read_text("utf-8")
        except (OSError, UnicodeError):
            continue
        summary = _section(text, "SUMMARY")
        handoff = _section(text, "PATCHSHUTTLE_AI_HANDOFF")
        if summary is not None and handoff is not None:
            return summary, handoff
    return "NOT_AVAILABLE", "NOT_AVAILABLE"


def _section(value: str, name: str) -> str | None:
    marker = f"=== {name} ===\n"
    start = value.find(marker)
    if start < 0:
        return None
    content_start = start + len(marker)
    end = value.find("\n=== ", content_start)
    return value[content_start : end if end >= 0 else None].strip()


def _bounded(value: str, maximum: int) -> tuple[str, bool]:
    raw = value.encode("utf-8")
    if len(raw) <= maximum:
        return value, False
    marker = _TRUNCATION.encode("utf-8")
    if maximum <= len(marker):
        return marker[:maximum].decode("utf-8"), True
    retained = raw[: max(0, maximum - len(marker))]
    return retained.decode("utf-8", errors="ignore") + _TRUNCATION, True


__all__ = [
    "HandoffResult",
    "SnapshotResult",
    "create_handoff",
    "create_snapshot",
]
