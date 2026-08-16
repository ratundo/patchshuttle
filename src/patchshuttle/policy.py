"""Workspace-relative path normalization and immutable local policy."""

from __future__ import annotations

import fnmatch
import ntpath
import os
import re
import stat
from dataclasses import dataclass, field
from enum import Enum
from functools import lru_cache
from os import PathLike
from pathlib import Path, PurePosixPath

from patchshuttle.errors import PolicyError, PolicyErrorCode
from patchshuttle.workspace import Workspace

_URL_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


class PathKind(str, Enum):
    """Observable filesystem kind returned by a successful policy check."""

    MISSING = "missing"
    FILE = "file"
    DIRECTORY = "directory"


@dataclass(frozen=True, slots=True)
class WorkspacePath:
    """One canonical workspace path and its inspected filesystem kind."""

    relative: PurePosixPath
    absolute: Path
    kind: PathKind

    @property
    def exists(self) -> bool:
        return self.kind is not PathKind.MISSING


@dataclass(frozen=True, slots=True)
class _GlobPattern:
    source: str
    segments: tuple[str, ...]

    def matches(self, path: PurePosixPath, *, case_sensitive: bool) -> bool:
        patterns = self.segments
        values = path.parts
        if not case_sensitive:
            patterns = tuple(part.casefold() for part in patterns)
            values = tuple(part.casefold() for part in values)

        @lru_cache(maxsize=None)
        def match(pattern_index: int, value_index: int) -> bool:
            if pattern_index == len(patterns):
                return value_index == len(values)

            pattern = patterns[pattern_index]
            if pattern == "**":
                return match(pattern_index + 1, value_index) or (
                    value_index < len(values) and match(pattern_index, value_index + 1)
                )

            return (
                value_index < len(values)
                and fnmatch.fnmatchcase(values[value_index], pattern)
                and match(pattern_index + 1, value_index + 1)
            )

        return match(0, 0)


def _platform_case_sensitive() -> bool:
    return os.path.normcase("A") != os.path.normcase("a")


@dataclass(frozen=True, slots=True)
class Policy:
    """Immutable workspace path policy derived from local configuration."""

    workspace: Workspace
    case_sensitive: bool = field(default_factory=_platform_case_sensitive)
    _protected_patterns: tuple[_GlobPattern, ...] = field(init=False, repr=False)
    _exception_patterns: tuple[_GlobPattern, ...] = field(init=False, repr=False)
    _ignored_patterns: tuple[_GlobPattern, ...] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        project = self.workspace.config.project
        object.__setattr__(
            self,
            "_protected_patterns",
            _compile_patterns(project.protected_paths),
        )
        object.__setattr__(
            self,
            "_exception_patterns",
            _compile_patterns(project.protected_path_exceptions),
        )
        object.__setattr__(
            self,
            "_ignored_patterns",
            _compile_patterns(project.ignored_paths),
        )

    @property
    def root(self) -> Path:
        return self.workspace.root

    def normalize(self, value: str | PathLike[str]) -> PurePosixPath:
        """Return a canonical relative path without touching the filesystem."""

        return _normalize_path(value)

    def is_protected(self, value: str | PathLike[str]) -> bool:
        """Report whether a canonical path is blocked from AI job access."""

        relative = self.normalize(value)
        return self._is_protected(relative)

    def is_ignored(self, value: str | PathLike[str]) -> bool:
        """Report whether inventory and audit traversal should skip a path."""

        relative = self.normalize(value)
        return any(
            pattern.matches(prefix, case_sensitive=self.case_sensitive)
            for prefix in _path_prefixes(relative)
            for pattern in self._ignored_patterns
        )

    def resolve(
        self,
        value: str | PathLike[str],
        *,
        allow_root: bool = False,
        allow_missing: bool = False,
    ) -> WorkspacePath:
        """Validate, inspect, and resolve one path without changing anything."""

        relative = self.normalize(value)
        display_path = relative.as_posix()
        if not relative.parts:
            if not allow_root:
                raise PolicyError(
                    PolicyErrorCode.PATH_ROOT_FORBIDDEN,
                    "workspace root requires explicit read-only permission",
                    path=display_path,
                )
            return WorkspacePath(
                relative=relative,
                absolute=self.root,
                kind=PathKind.DIRECTORY,
            )

        if self._is_protected(relative):
            raise PolicyError(
                PolicyErrorCode.PATH_PROTECTED,
                "path is protected by local policy",
                path=display_path,
            )

        candidate = self.root.joinpath(*relative.parts)
        kind = self._inspect(relative)
        resolved = self._resolve_candidate(candidate, display_path)
        if not _is_within_root(
            self.root,
            resolved,
            case_sensitive=self.case_sensitive,
        ):
            raise PolicyError(
                PolicyErrorCode.PATH_OUTSIDE_WORKSPACE,
                "resolved path escapes the workspace root",
                path=display_path,
            )
        if not _paths_equal(
            candidate,
            resolved,
            case_sensitive=self.case_sensitive,
        ):
            raise PolicyError(
                PolicyErrorCode.PATH_SYMLINK,
                "path changed or resolved through a symbolic link",
                path=display_path,
            )

        if kind is PathKind.MISSING and not allow_missing:
            raise PolicyError(
                PolicyErrorCode.PATH_NOT_FOUND,
                "path does not exist",
                path=display_path,
            )

        return WorkspacePath(relative=relative, absolute=resolved, kind=kind)

    def _is_protected(self, relative: PurePosixPath) -> bool:
        if not relative.parts:
            return True

        first = relative.parts[0]
        patches_name = "patches" if self.case_sensitive else "patches".casefold()
        compared_first = first if self.case_sensitive else first.casefold()
        if compared_first == patches_name:
            return True

        if any(
            pattern.matches(relative, case_sensitive=self.case_sensitive)
            for pattern in self._exception_patterns
        ):
            return False
        return any(
            pattern.matches(prefix, case_sensitive=self.case_sensitive)
            for prefix in _path_prefixes(relative)
            for pattern in self._protected_patterns
        )

    def _inspect(self, relative: PurePosixPath) -> PathKind:
        current = self.root
        for index, part in enumerate(relative.parts):
            current = current / part
            current_relative = PurePosixPath(*relative.parts[: index + 1]).as_posix()
            try:
                mode = current.lstat().st_mode
            except FileNotFoundError:
                return PathKind.MISSING
            except NotADirectoryError as exc:
                raise PolicyError(
                    PolicyErrorCode.PATH_PARENT_NOT_DIRECTORY,
                    "an existing parent is not a directory",
                    path=PurePosixPath(*relative.parts[:index]).as_posix(),
                ) from exc
            except OSError as exc:
                raise PolicyError(
                    PolicyErrorCode.PATH_INSPECTION_FAILED,
                    "path metadata could not be read",
                    path=current_relative,
                ) from exc

            if stat.S_ISLNK(mode):
                raise PolicyError(
                    PolicyErrorCode.PATH_SYMLINK,
                    "symbolic-link targets and parents are not allowed",
                    path=current_relative,
                )

            is_target = index == len(relative.parts) - 1
            if not is_target:
                if not stat.S_ISDIR(mode):
                    raise PolicyError(
                        PolicyErrorCode.PATH_PARENT_NOT_DIRECTORY,
                        "an existing parent is not a directory",
                        path=current_relative,
                    )
                continue

            if stat.S_ISREG(mode):
                return PathKind.FILE
            if stat.S_ISDIR(mode):
                return PathKind.DIRECTORY
            raise PolicyError(
                PolicyErrorCode.PATH_SPECIAL_FILE,
                "device files, sockets, and named pipes are not allowed",
                path=current_relative,
            )

        return PathKind.MISSING  # pragma: no cover - root handled by resolve

    @staticmethod
    def _resolve_candidate(candidate: Path, display_path: str) -> Path:
        try:
            return candidate.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise PolicyError(
                PolicyErrorCode.PATH_INSPECTION_FAILED,
                "path could not be resolved",
                path=display_path,
            ) from exc


def _normalize_path(value: str | PathLike[str]) -> PurePosixPath:
    try:
        raw = os.fspath(value)
    except TypeError as exc:
        raise PolicyError(
            PolicyErrorCode.PATH_INVALID,
            "path must be text or a text path-like value",
        ) from exc

    if not isinstance(raw, str) or not raw or "\0" in raw:
        raise PolicyError(
            PolicyErrorCode.PATH_INVALID,
            "path must be non-empty text without null bytes",
        )

    drive, _ = ntpath.splitdrive(raw)
    if drive or raw.startswith(("/", "\\")):
        raise PolicyError(
            PolicyErrorCode.PATH_ABSOLUTE,
            "absolute and drive-qualified paths are not allowed",
            path=raw,
        )
    if _URL_SCHEME.match(raw):
        raise PolicyError(
            PolicyErrorCode.PATH_URL,
            "URL-like paths are not allowed",
            path=raw,
        )

    parts: list[str] = []
    for part in raw.replace("\\", "/").split("/"):
        if part == "..":
            raise PolicyError(
                PolicyErrorCode.PATH_TRAVERSAL,
                "parent traversal is not allowed",
                path=raw,
            )
        if part not in ("", "."):
            parts.append(part)
    return PurePosixPath(*parts) if parts else PurePosixPath(".")


def _compile_patterns(patterns: tuple[str, ...]) -> tuple[_GlobPattern, ...]:
    compiled: list[_GlobPattern] = []
    for pattern in patterns:
        try:
            normalized = _normalize_path(pattern)
        except PolicyError as exc:
            raise PolicyError(
                PolicyErrorCode.POLICY_PATTERN_INVALID,
                "local policy pattern must be a relative workspace glob",
                path=pattern,
            ) from exc
        compiled.append(_GlobPattern(source=pattern, segments=tuple(normalized.parts)))
    return tuple(compiled)


def _path_prefixes(path: PurePosixPath) -> tuple[PurePosixPath, ...]:
    if not path.parts:
        return (path,)
    return tuple(
        PurePosixPath(*path.parts[:index]) for index in range(1, len(path.parts) + 1)
    )


def _is_within_root(
    root: Path,
    candidate: Path,
    *,
    case_sensitive: bool,
) -> bool:
    root_text = str(root)
    candidate_text = str(candidate)
    if not case_sensitive:
        root_text = root_text.casefold()
        candidate_text = candidate_text.casefold()
    try:
        common = os.path.commonpath((root_text, candidate_text))
    except ValueError:
        return False
    return common == root_text


def _paths_equal(
    left: Path,
    right: Path,
    *,
    case_sensitive: bool,
) -> bool:
    left_text = os.path.normpath(str(left))
    right_text = os.path.normpath(str(right))
    if not case_sensitive:
        left_text = left_text.casefold()
        right_text = right_text.casefold()
    return left_text == right_text


__all__ = ["PathKind", "Policy", "WorkspacePath"]
