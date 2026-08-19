"""Strict in-process unified-diff parsing and dry-run application."""

from __future__ import annotations

import re
from dataclasses import dataclass
from itertools import zip_longest
from pathlib import PurePosixPath

from patchshuttle.errors import PlanningError, PlanningErrorCode

_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?:.*)?(?:\n)?$")
_BINARY_MARKERS = ("Binary files ", "GIT binary patch")
_FORBIDDEN_METADATA = (
    "new file mode ",
    "deleted file mode ",
    "old mode ",
    "new mode ",
    "similarity index ",
    "rename from ",
    "rename to ",
    "copy from ",
    "copy to ",
)


@dataclass(frozen=True, slots=True)
class DiffHunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    old_lines: tuple[str, ...]
    new_lines: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FileDiff:
    path: str
    hunks: tuple[DiffHunk, ...]


def parse_unified_diff(
    value: str,
    *,
    strip: int,
    item_id: str,
) -> tuple[FileDiff, ...]:
    """Parse a text-only, existing-file unified diff without side effects."""

    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.splitlines(keepends=True)
    if not lines:
        raise _error(
            PlanningErrorCode.DIFF_INVALID,
            "unified diff is empty",
            item_id,
        )

    parsed: list[FileDiff] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith(_BINARY_MARKERS):
            raise _error(
                PlanningErrorCode.DIFF_BINARY_FORBIDDEN,
                "binary diffs are not allowed",
                item_id,
            )
        if line.startswith(_FORBIDDEN_METADATA):
            raise _error(
                PlanningErrorCode.DIFF_PATH_INVALID,
                "file creation, deletion, rename, copy, and mode changes are forbidden",
                item_id,
            )
        if line.startswith("diff --git ") or line.startswith("index "):
            index += 1
            continue
        if not line.startswith("--- "):
            raise _error(
                PlanningErrorCode.DIFF_INVALID,
                "expected an old-file header beginning with '--- '",
                item_id,
            )

        old_path = _parse_header_path(line, strip=strip, item_id=item_id)
        index += 1
        if index >= len(lines) or not lines[index].startswith("+++ "):
            raise _error(
                PlanningErrorCode.DIFF_INVALID,
                "old-file header must be followed by a new-file header",
                item_id,
            )
        new_path = _parse_header_path(lines[index], strip=strip, item_id=item_id)
        if old_path != new_path:
            raise _error(
                PlanningErrorCode.DIFF_PATH_INVALID,
                "unified diff may not rename or copy files",
                item_id,
                path=new_path,
            )
        index += 1

        hunks: list[DiffHunk] = []
        while index < len(lines) and lines[index].startswith("@@ "):
            hunk, index = _parse_hunk(
                lines,
                index,
                item_id=item_id,
                hunk_number=len(hunks) + 1,
            )
            hunks.append(hunk)
        if not hunks:
            raise _error(
                PlanningErrorCode.DIFF_INVALID,
                "each file diff requires at least one hunk",
                item_id,
                path=new_path,
            )
        if any(existing.path == new_path for existing in parsed):
            raise _error(
                PlanningErrorCode.DIFF_INVALID,
                "a unified diff may contain each target file only once",
                item_id,
                path=new_path,
            )
        parsed.append(FileDiff(path=new_path, hunks=tuple(hunks)))

    if not parsed:
        raise _error(
            PlanningErrorCode.DIFF_INVALID,
            "unified diff does not contain a file patch",
            item_id,
        )
    return tuple(parsed)


def apply_file_diff(text: str, diff: FileDiff, *, item_id: str) -> str:
    """Apply parsed hunks to normalized LF text in memory."""

    source = text.splitlines(keepends=True)
    output: list[str] = []
    cursor = 0
    for hunk_number, hunk in enumerate(diff.hunks, start=1):
        start = hunk.old_start if hunk.old_count == 0 else hunk.old_start - 1
        end = start + len(hunk.old_lines)
        if start < cursor or end > len(source):
            reason = "overlaps a previous hunk" if start < cursor else "is out of range"
            raise _error(
                PlanningErrorCode.DIFF_HUNK_MISMATCH,
                f"hunk {hunk_number} {reason} for the current file",
                item_id,
                path=diff.path,
                details=(
                    f"  hunk: {hunk_number}",
                    f"  declared_old_start: {hunk.old_start}",
                    f"  declared_old_lines: {hunk.old_count}",
                    f"  source_lines: {len(source)}",
                    f"  previous_hunk_end: {cursor}",
                ),
            )
        actual_lines = tuple(source[start:end])
        if actual_lines != hunk.old_lines:
            mismatch = _first_context_mismatch(
                hunk.old_lines,
                actual_lines,
                first_line=hunk.old_start,
            )
            raise _error(
                PlanningErrorCode.DIFF_HUNK_MISMATCH,
                f"hunk {hunk_number} context does not match the current file",
                item_id,
                path=diff.path,
                details=(f"  hunk: {hunk_number}", *mismatch),
            )
        output.extend(source[cursor:start])
        output.extend(hunk.new_lines)
        cursor = end
    output.extend(source[cursor:])
    return "".join(output)


def _parse_header_path(line: str, *, strip: int, item_id: str) -> str:
    raw = line[4:].rstrip("\n").split("\t", 1)[0]
    if raw == "/dev/null":
        raise _error(
            PlanningErrorCode.DIFF_PATH_INVALID,
            "file creation and deletion diffs are not allowed",
            item_id,
        )
    if not raw or raw.startswith('"'):
        raise _error(
            PlanningErrorCode.DIFF_PATH_INVALID,
            "quoted or empty diff paths are not supported",
            item_id,
        )
    if raw.startswith(("/", "\\")):
        raise _error(
            PlanningErrorCode.DIFF_PATH_INVALID,
            "absolute diff paths are not allowed",
            item_id,
            path=raw,
        )

    parts = raw.replace("\\", "/").split("/")
    if len(parts) <= strip:
        raise _error(
            PlanningErrorCode.DIFF_PATH_INVALID,
            f"diff path has fewer than {strip + 1} component(s)",
            item_id,
            path=raw,
        )
    stripped = PurePosixPath(*parts[strip:]).as_posix()
    return stripped


def _parse_hunk(
    lines: list[str],
    index: int,
    *,
    item_id: str,
    hunk_number: int,
) -> tuple[DiffHunk, int]:
    match = _HUNK_HEADER.fullmatch(lines[index])
    if match is None:
        raise _error(
            PlanningErrorCode.DIFF_INVALID,
            f"invalid unified-diff hunk {hunk_number} header",
            item_id,
        )
    old_start = int(match.group(1))
    old_count = int(match.group(2) or 1)
    new_start = int(match.group(3))
    new_count = int(match.group(4) or 1)
    if (old_start == 0 and old_count != 0) or (new_start == 0 and new_count != 0):
        raise _error(
            PlanningErrorCode.DIFF_INVALID,
            "a zero hunk start is valid only for an empty range",
            item_id,
        )
    index += 1
    old_lines: list[str] = []
    new_lines: list[str] = []
    previous_prefix: str | None = None

    while index < len(lines):
        line = lines[index]
        if line.startswith(("@@ ", "--- ", "diff --git ")):
            break
        if line.startswith(_BINARY_MARKERS) or line.startswith(_FORBIDDEN_METADATA):
            break
        if line.startswith("\\"):
            if line.rstrip("\n") != "\\ No newline at end of file":
                raise _error(
                    PlanningErrorCode.DIFF_INVALID,
                    "unknown backslash marker in unified diff",
                    item_id,
                )
            _remove_last_newline(
                old_lines,
                new_lines,
                previous_prefix=previous_prefix,
                item_id=item_id,
            )
            index += 1
            continue
        if not line or line[0] not in " +-":
            raise _error(
                PlanningErrorCode.DIFF_INVALID,
                "hunk lines must begin with space, '+', or '-'",
                item_id,
            )

        previous_prefix = line[0]
        content = line[1:]
        if previous_prefix in " -":
            old_lines.append(content)
        if previous_prefix in " +":
            new_lines.append(content)
        index += 1

    if len(old_lines) != old_count or len(new_lines) != new_count:
        raise _error(
            PlanningErrorCode.DIFF_INVALID,
            f"hunk {hunk_number} body line counts do not match its header",
            item_id,
            details=(
                f"  hunk: {hunk_number}",
                f"  declared_old_lines: {old_count}",
                f"  actual_old_lines: {len(old_lines)}",
                f"  declared_new_lines: {new_count}",
                f"  actual_new_lines: {len(new_lines)}",
            ),
        )
    return (
        DiffHunk(
            old_start=old_start,
            old_count=old_count,
            new_start=new_start,
            new_count=new_count,
            old_lines=tuple(old_lines),
            new_lines=tuple(new_lines),
        ),
        index,
    )


def _remove_last_newline(
    old_lines: list[str],
    new_lines: list[str],
    *,
    previous_prefix: str | None,
    item_id: str,
) -> None:
    if previous_prefix is None:
        raise _error(
            PlanningErrorCode.DIFF_INVALID,
            "no-newline marker must follow a hunk content line",
            item_id,
        )
    targets = []
    if previous_prefix in " -":
        targets.append(old_lines)
    if previous_prefix in " +":
        targets.append(new_lines)
    for target in targets:
        if not target or not target[-1].endswith("\n"):
            raise _error(
                PlanningErrorCode.DIFF_INVALID,
                "no-newline marker is duplicated or misplaced",
                item_id,
            )
        target[-1] = target[-1][:-1]


def _error(
    code: PlanningErrorCode,
    message: str,
    item_id: str,
    *,
    path: str | None = None,
    details: tuple[str, ...] = (),
) -> PlanningError:
    return PlanningError(
        code,
        message,
        item_id=item_id,
        path=path,
        details=details,
    )


def _first_context_mismatch(
    expected: tuple[str, ...],
    actual: tuple[str, ...],
    *,
    first_line: int,
) -> tuple[str, ...]:
    for offset, (expected_line, actual_line) in enumerate(
        zip_longest(expected, actual, fillvalue=None)
    ):
        if expected_line == actual_line:
            continue
        return (
            f"  first_mismatch_line: {first_line + offset}",
            f"  expected: {_preview_line(expected_line)}",
            f"  actual: {_preview_line(actual_line)}",
        )
    return ("  first_mismatch_line: unknown",)


def _preview_line(value: str | None, maximum: int = 240) -> str:
    rendered = "<missing>" if value is None else repr(value.rstrip("\n"))
    return rendered if len(rendered) <= maximum else rendered[: maximum - 3] + "..."


__all__ = ["FileDiff", "apply_file_diff", "parse_unified_diff"]
