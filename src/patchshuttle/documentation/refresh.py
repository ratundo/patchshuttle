"""Reviewed refresh of generated AI guidance with preserved appended notes."""

from __future__ import annotations

import hashlib
import os
import stat
import uuid
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from patchshuttle.runner import acquire_workspace_lock
from patchshuttle.workspace import Workspace

MAX_GUIDE_BYTES = 1_000_000
LEGACY_GUIDES = (
    (348, "4cd8db31be8c80da2168004d66ce326f4fb8b5c386fb99cd62d5a3445daa1824"),
    (372, "74ce51476f7bc57786afdfac1def467a9bb287e35156a854fd1d687c51cc3666"),
    (455, "44c7e4263a72b8d3276f059233fee1aa476aeeef189a36703b03dffbbde74116"),
)


@dataclass(frozen=True)
class GuidePlan:
    original: bytes
    replacement: bytes
    expected_plan: str
    status: str


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _regular(path: Path) -> None:
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError("guide or backup must be a regular file")


def _directories(workspace: Workspace) -> None:
    for path in (workspace.patches_dir, workspace.patches_dir / "state"):
        if not stat.S_ISDIR(path.lstat().st_mode):
            raise ValueError("managed parent must be a real directory")


def plan_guide_refresh(workspace: Workspace) -> GuidePlan:
    """Read only; refuse unknown inline modifications rather than guessing a merge."""
    _directories(workspace)
    path = workspace.patches_dir / "AI_GUIDE.md"
    _regular(path)
    with path.open("rb") as stream:
        original = stream.read(MAX_GUIDE_BYTES + 1)
    if len(original) > MAX_GUIDE_BYTES:
        raise ValueError("AI guide exceeds the refresh size limit")
    text = original.decode("utf-8-sig")
    template = (
        resources.files("patchshuttle.resources")
        .joinpath("AI_GUIDE.md")
        .read_text(encoding="utf-8")
    )
    rendered = template.replace("{{PROJECT_ID}}", workspace.project_id)
    lines = text.splitlines(keepends=True)
    records = (
        (len(template.splitlines(keepends=True)), _digest(template.encode())),
        *LEGACY_GUIDES,
    )
    for count, digest in records:
        prefix = "".join(lines[:count])
        canonical = prefix.replace("\r\n", "\n").replace(
            workspace.project_id, "{{PROJECT_ID}}"
        )
        if _digest(canonical.encode()) == digest:
            newline = "\r\n" if "\r\n" in prefix else "\n"
            bom = b"\xef\xbb\xbf" if original.startswith(b"\xef\xbb\xbf") else b""
            replacement = bom + (
                rendered.replace("\n", newline) + "".join(lines[count:])
            ).encode("utf-8")
            identity = (
                workspace.project_id.encode() + b"\0" + original + b"\0" + replacement
            )
            return GuidePlan(
                original,
                replacement,
                _digest(identity),
                "CURRENT" if replacement == original else "UPDATE_AVAILABLE",
            )
    raise ValueError(
        "MANUAL_REVIEW_REQUIRED: guide has unknown or inline edits; preserve and reconcile them manually"
    )


def apply_guide_refresh(workspace: Workspace, expected_plan: str) -> str:
    """Revalidate under the workspace lock, back up exact bytes, replace atomically."""
    _directories(workspace)
    with acquire_workspace_lock(workspace):
        plan = plan_guide_refresh(workspace)
        if expected_plan != plan.expected_plan:
            raise ValueError(
                "GUIDE_PLAN_MISMATCH: run docs refresh again and review the new preview"
            )
        if plan.status == "CURRENT":
            return "CURRENT"
        path = workspace.patches_dir / "AI_GUIDE.md"
        backup = (
            workspace.patches_dir
            / "state"
            / ("ai-guide-" + _digest(plan.original) + ".bak")
        )
        if backup.exists() or backup.is_symlink():
            _regular(backup)
            if backup.read_bytes() != plan.original:
                raise ValueError("guide backup does not match original bytes")
        else:
            with backup.open("xb") as stream:
                stream.write(plan.original)
                stream.flush()
                os.fsync(stream.fileno())
        temporary = workspace.patches_dir / (".ai-guide-" + uuid.uuid4().hex + ".tmp")
        stream = temporary.open("xb")
        try:
            with stream:
                stream.write(plan.replacement)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, stat.S_IMODE(path.stat().st_mode))
            if plan_guide_refresh(workspace).expected_plan != expected_plan:
                raise ValueError("GUIDE_PLAN_MISMATCH: guide changed during refresh")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return "UPDATED"
