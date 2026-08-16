"""Declarative public action constructors."""

from __future__ import annotations

from patchshuttle.models import Action


def tree(
    path: str = ".",
    *,
    depth: int = 4,
    max_entries: int = 500,
    include_hidden: bool = False,
) -> Action:
    return Action(
        {
            "tree": {
                "path": path,
                "depth": depth,
                "max_entries": max_entries,
                "include_hidden": include_hidden,
            }
        }
    )


def read(
    path: str,
    *,
    start_line: int = 1,
    end_line: int | None = None,
    max_bytes: int | None = None,
) -> Action:
    parameters = {"path": path, "start_line": start_line}
    if end_line is not None:
        parameters["end_line"] = end_line
    if max_bytes is not None:
        parameters["max_bytes"] = max_bytes
    return Action({"read": parameters})


def search(
    text: str,
    *,
    path: str = ".",
    glob: str | None = None,
    case_sensitive: bool = True,
    max_results: int = 200,
) -> Action:
    parameters = {
        "path": path,
        "text": text,
        "case_sensitive": case_sensitive,
        "max_results": max_results,
    }
    if glob is not None:
        parameters["glob"] = glob
    return Action({"search": parameters})


def find_files(
    glob: str,
    *,
    path: str = ".",
    max_results: int = 500,
) -> Action:
    return Action(
        {
            "find_files": {
                "path": path,
                "glob": glob,
                "max_results": max_results,
            }
        }
    )


def file_info(path: str) -> Action:
    return Action({"file_info": {"path": path}})


def hash(path: str, *, algorithm: str = "sha256") -> Action:
    return Action({"hash": {"path": path, "algorithm": algorithm}})


def git_status() -> Action:
    return Action({"git_status": {}})


def environment() -> Action:
    return Action({"environment": {}})


def create_directory(path: str) -> Action:
    return Action({"create_directory": {"path": path}})


def create_file(
    path: str,
    content: str,
    *,
    encoding: str = "utf-8",
    newline: str = "lf",
) -> Action:
    return Action(
        {
            "create_file": {
                "path": path,
                "content": content,
                "encoding": encoding,
                "newline": newline,
            }
        }
    )


def replace_exact(
    path: str,
    old: str,
    new: str,
    *,
    expected_count: int = 1,
) -> Action:
    return Action(
        {
            "replace_exact": {
                "path": path,
                "old": old,
                "new": new,
                "expected_count": expected_count,
            }
        }
    )


def insert_before(
    path: str,
    anchor: str,
    content: str,
    *,
    expected_count: int = 1,
) -> Action:
    return Action(
        {
            "insert_before": {
                "path": path,
                "anchor": anchor,
                "content": content,
                "expected_count": expected_count,
            }
        }
    )


def insert_after(
    path: str,
    anchor: str,
    content: str,
    *,
    expected_count: int = 1,
) -> Action:
    return Action(
        {
            "insert_after": {
                "path": path,
                "anchor": anchor,
                "content": content,
                "expected_count": expected_count,
            }
        }
    )


def delete_exact(
    path: str,
    text: str,
    *,
    expected_count: int = 1,
) -> Action:
    return Action(
        {
            "delete_exact": {
                "path": path,
                "text": text,
                "expected_count": expected_count,
            }
        }
    )


def apply_diff(diff: str, *, strip: int = 1) -> Action:
    return Action({"apply_diff": {"diff": diff, "strip": strip}})


__all__ = [
    "apply_diff",
    "create_directory",
    "create_file",
    "delete_exact",
    "environment",
    "file_info",
    "find_files",
    "git_status",
    "hash",
    "insert_after",
    "insert_before",
    "read",
    "replace_exact",
    "search",
    "tree",
]
