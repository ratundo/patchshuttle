"""Conservative cleanup of Python caches created during project checks."""

from __future__ import annotations

import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from patchshuttle.planner import Plan


@dataclass(frozen=True, slots=True)
class RuntimeCacheLedger:
    """Exact cache paths observed after actions and before executable checks."""

    roots: tuple[PurePosixPath, ...]
    directories: frozenset[PurePosixPath]
    non_directories: frozenset[PurePosixPath]
    entries: frozenset[PurePosixPath]


@dataclass(frozen=True, slots=True)
class RuntimeCacheCleanup:
    """Safe cleanup result for cache entries absent from the ledger."""

    removed_files: tuple[PurePosixPath, ...]
    removed_directories: tuple[PurePosixPath, ...]
    unresolved: tuple[PurePosixPath, ...]

    @property
    def success(self) -> bool:
        return not self.unresolved


class RuntimeCacheError(OSError):
    """A bounded runtime-cache path could not be inspected safely."""

    def __init__(self, message: str, *, path: PurePosixPath) -> None:
        self.path = path
        super().__init__(message)


def capture_runtime_cache_ledger(plan: Plan) -> RuntimeCacheLedger:
    """Capture direct ``__pycache__`` entries near changed Python paths."""

    roots = _cache_roots(plan)
    directories: set[PurePosixPath] = set()
    non_directories: set[PurePosixPath] = set()
    entries: set[PurePosixPath] = set()
    observed = 0
    maximum = plan.workspace.config.execution.max_inventory_entries
    for root in roots:
        cache = root / "__pycache__"
        absolute = _absolute(plan.workspace.root, cache)
        try:
            metadata = absolute.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise RuntimeCacheError(
                "runtime cache metadata could not be inspected",
                path=cache,
            ) from exc
        if not stat.S_ISDIR(metadata.st_mode):
            non_directories.add(cache)
            continue
        directories.add(cache)
        try:
            children = tuple(absolute.iterdir())
        except OSError as exc:
            raise RuntimeCacheError(
                "runtime cache directory could not be inspected",
                path=cache,
            ) from exc
        for child in children:
            observed += 1
            relative = cache / child.name
            if observed > maximum:
                raise RuntimeCacheError(
                    "runtime cache entry limit was exceeded",
                    path=relative,
                )
            try:
                child_metadata = child.lstat()
            except OSError as exc:
                raise RuntimeCacheError(
                    "runtime cache entry could not be inspected",
                    path=relative,
                ) from exc
            entries.add(relative)
    return RuntimeCacheLedger(
        roots=roots,
        directories=frozenset(directories),
        non_directories=frozenset(non_directories),
        entries=frozenset(entries),
    )


def cleanup_runtime_caches(
    plan: Plan, ledger: RuntimeCacheLedger
) -> RuntimeCacheCleanup:
    """Remove only new regular ``.pyc`` files and newly empty cache directories."""

    if ledger.roots != _cache_roots(plan):
        raise ValueError("runtime cache ledger scope does not match the plan")
    removed_files: list[PurePosixPath] = []
    removed_directories: list[PurePosixPath] = []
    unresolved: list[PurePosixPath] = []
    for root in reversed(ledger.roots):
        cache = root / "__pycache__"
        absolute = _absolute(plan.workspace.root, cache)
        try:
            metadata = absolute.lstat()
        except FileNotFoundError:
            if cache in ledger.directories or cache in ledger.non_directories:
                unresolved.append(cache)
            continue
        except OSError:
            unresolved.append(cache)
            continue
        if not stat.S_ISDIR(metadata.st_mode):
            if cache not in ledger.non_directories:
                unresolved.append(cache)
            continue
        if cache in ledger.non_directories:
            unresolved.append(cache)
            continue
        try:
            children = tuple(sorted(absolute.iterdir(), key=lambda item: item.name))
        except OSError:
            unresolved.append(cache)
            continue
        for child in children:
            relative = cache / child.name
            if relative in ledger.entries:
                continue
            try:
                child_metadata = child.lstat()
                if not stat.S_ISREG(child_metadata.st_mode) or child.suffix != ".pyc":
                    unresolved.append(relative)
                    continue
                child.unlink()
                removed_files.append(relative)
            except OSError:
                unresolved.append(relative)
        if cache in ledger.directories:
            continue
        try:
            absolute.rmdir()
            removed_directories.append(cache)
        except OSError:
            unresolved.append(cache)
    return RuntimeCacheCleanup(
        removed_files=tuple(removed_files),
        removed_directories=tuple(removed_directories),
        unresolved=tuple(dict.fromkeys(unresolved)),
    )


def _cache_roots(plan: Plan) -> tuple[PurePosixPath, ...]:
    roots: set[PurePosixPath] = set()
    for change in plan.file_changes:
        if change.path.suffix != ".py":
            continue
        parent = change.path.parent
        while parent.parts:
            roots.add(parent)
            parent = parent.parent
        roots.add(PurePosixPath())
    return tuple(sorted(roots, key=lambda path: (len(path.parts), path.as_posix())))


def _absolute(root: Path, relative: PurePosixPath) -> Path:
    return root.joinpath(*relative.parts)


__all__ = [
    "RuntimeCacheCleanup",
    "RuntimeCacheError",
    "RuntimeCacheLedger",
    "capture_runtime_cache_ledger",
    "cleanup_runtime_caches",
]
