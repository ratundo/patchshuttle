"""Canonical, platform-stable line-range selection helpers."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass


class LineRangeBoundsError(ValueError):
    """Raised when a requested inclusive range is outside the current text."""

    def __init__(self, start_line: int, end_line: int, total_lines: int) -> None:
        self.start_line = start_line
        self.end_line = end_line
        self.total_lines = total_lines
        super().__init__(
            f"line range {start_line}-{end_line} is outside a {total_lines}-line file"
        )


@dataclass(frozen=True, slots=True)
class CanonicalLineRange:
    """One inclusive range in LF-normalized text with stable offsets and hash."""

    start_line: int
    end_line: int
    total_lines: int
    start_offset: int
    end_offset: int
    content: str
    sha256: str

    @property
    def ends_with_newline(self) -> bool:
        """Whether the selected physical range includes its final LF."""

        return self.content.endswith("\n")


def normalize_newlines(value: str) -> str:
    """Return the canonical LF representation used by text actions."""

    return value.replace("\r\n", "\n").replace("\r", "\n")


def canonical_lines(value: str) -> tuple[str, ...]:
    """Split text into physical LF lines while retaining each existing LF."""

    normalized = normalize_newlines(value)
    if not normalized:
        return ()
    parts = normalized.split("\n")
    lines = [part + "\n" for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return tuple(lines)


def canonical_sha256(value: str) -> str:
    """Hash canonical LF text as UTF-8 bytes."""

    return hashlib.sha256(normalize_newlines(value).encode("utf-8")).hexdigest()


def select_line_range(
    value: str,
    *,
    start_line: int,
    end_line: int,
) -> CanonicalLineRange:
    """Select one 1-based inclusive physical-line range without approximation."""

    normalized = normalize_newlines(value)
    lines = canonical_lines(normalized)
    total_lines = len(lines)
    if (
        start_line < 1
        or end_line < start_line
        or start_line > total_lines
        or end_line > total_lines
    ):
        raise LineRangeBoundsError(start_line, end_line, total_lines)

    start_offset = sum(len(line) for line in lines[: start_line - 1])
    content = "".join(lines[start_line - 1 : end_line])
    end_offset = start_offset + len(content)
    return CanonicalLineRange(
        start_line=start_line,
        end_line=end_line,
        total_lines=total_lines,
        start_offset=start_offset,
        end_offset=end_offset,
        content=content,
        sha256=canonical_sha256(content),
    )


__all__ = [
    "CanonicalLineRange",
    "LineRangeBoundsError",
    "canonical_lines",
    "canonical_sha256",
    "normalize_newlines",
    "select_line_range",
]
