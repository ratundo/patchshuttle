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


def search_context(
    text: str,
    *,
    path: str = ".",
    glob: str | None = None,
    case_sensitive: bool = True,
    max_results: int = 200,
    before: int = 3,
    after: int = 3,
) -> Action:
    parameters = {
        "path": path,
        "text": text,
        "case_sensitive": case_sensitive,
        "max_results": max_results,
        "before": before,
        "after": after,
    }
    if glob is not None:
        parameters["glob"] = glob
    return Action({"search_context": parameters})


def read_symbol(
    path: str,
    symbol: str,
    *,
    max_bytes: int | None = None,
    include_template: bool = False,
) -> Action:
    parameters = {"path": path, "symbol": symbol, "include_template": include_template}
    if max_bytes is not None:
        parameters["max_bytes"] = max_bytes
    return Action({"read_symbol": parameters})


def python_structure(
    path: str = ".",
    *,
    max_files: int = 300,
    max_symbols: int = 2000,
    compact: bool = False,
) -> Action:
    return Action(
        {
            "python_structure": {
                "path": path,
                "max_files": max_files,
                "max_symbols": max_symbols,
                "compact": compact,
            }
        }
    )


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


def hash_range(
    path: str,
    start_line: int,
    end_line: int,
    *,
    algorithm: str = "sha256",
) -> Action:
    return Action(
        {
            "hash_range": {
                "path": path,
                "start_line": start_line,
                "end_line": end_line,
                "algorithm": algorithm,
            }
        }
    )


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


def replace_symbol(
    path: str,
    symbol: str,
    new_content: str,
    *,
    expected_sha256: str | None = None,
    expected_ast_sha256: str | None = None,
) -> Action:
    return Action(
        {
            "replace_symbol": {
                "path": path,
                "symbol": symbol,
                "new_content": new_content,
                "expected_sha256": expected_sha256,
                "expected_ast_sha256": expected_ast_sha256,
            }
        }
    )


def move_file(path: str, destination: str, *, expected_sha256: str) -> Action:
    return Action(
        {
            "move_file": {
                "path": path,
                "destination": destination,
                "expected_sha256": expected_sha256,
            }
        }
    )


def move_range(
    path: str,
    destination: str,
    *,
    start_line: int,
    end_line: int,
    destination_line: int,
    expected_destination_sha256: str,
    expected_content: str | None = None,
    expected_sha256: str | None = None,
) -> Action:
    return Action(
        {
            "move_range": {
                "path": path,
                "destination": destination,
                "start_line": start_line,
                "end_line": end_line,
                "destination_line": destination_line,
                "expected_destination_sha256": expected_destination_sha256,
                "expected_content": expected_content,
                "expected_sha256": expected_sha256,
            }
        }
    )


def move_symbol(
    path: str,
    symbol: str,
    destination: str,
    *,
    expected_ast_sha256: str,
    expected_destination_sha256: str,
    destination_class: str | None = None,
) -> Action:
    return Action(
        {
            "move_symbol": {
                "path": path,
                "symbol": symbol,
                "destination": destination,
                "expected_ast_sha256": expected_ast_sha256,
                "expected_destination_sha256": expected_destination_sha256,
                "destination_class": destination_class,
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


def replace_range(
    path: str,
    start_line: int,
    end_line: int,
    new_content: str,
    *,
    expected_content: str | None = None,
    expected_sha256: str | None = None,
) -> Action:
    parameters = _guarded_range_parameters(
        path,
        start_line=start_line,
        end_line=end_line,
        expected_content=expected_content,
        expected_sha256=expected_sha256,
    )
    parameters["new_content"] = new_content
    return Action({"replace_range": parameters})


def delete_range(
    path: str,
    start_line: int,
    end_line: int,
    *,
    expected_content: str | None = None,
    expected_sha256: str | None = None,
) -> Action:
    return Action(
        {
            "delete_range": _guarded_range_parameters(
                path,
                start_line=start_line,
                end_line=end_line,
                expected_content=expected_content,
                expected_sha256=expected_sha256,
            )
        }
    )


def insert_at_line(
    path: str,
    line: int,
    position: str,
    content: str,
    *,
    expected_content: str | None = None,
    expected_sha256: str | None = None,
) -> Action:
    parameters: dict[str, object] = {
        "path": path,
        "line": line,
        "position": position,
        "content": content,
    }
    _add_guards(
        parameters,
        expected_content=expected_content,
        expected_sha256=expected_sha256,
    )
    return Action({"insert_at_line": parameters})


def apply_diff(diff: str, *, strip: int = 1) -> Action:
    return Action({"apply_diff": {"diff": diff, "strip": strip}})


def _guarded_range_parameters(
    path: str,
    *,
    start_line: int,
    end_line: int,
    expected_content: str | None,
    expected_sha256: str | None,
) -> dict[str, object]:
    parameters: dict[str, object] = {
        "path": path,
        "start_line": start_line,
        "end_line": end_line,
    }
    _add_guards(
        parameters,
        expected_content=expected_content,
        expected_sha256=expected_sha256,
    )
    return parameters


def _add_guards(
    parameters: dict[str, object],
    *,
    expected_content: str | None,
    expected_sha256: str | None,
) -> None:
    if expected_content is not None:
        parameters["expected_content"] = expected_content
    if expected_sha256 is not None:
        parameters["expected_sha256"] = expected_sha256


def read_imports(
    path: str, *, max_bytes: int | None = None, include_template: bool = False
) -> Action:
    parameters = {"path": path, "include_template": include_template}
    if max_bytes is not None:
        parameters["max_bytes"] = max_bytes
    return Action({"read_imports": parameters})


def ensure_import(
    path: str, statement: str, *, expected_imports_ast_sha256: str
) -> Action:
    return Action(
        {
            "ensure_import": {
                "path": path,
                "statement": statement,
                "expected_imports_ast_sha256": expected_imports_ast_sha256,
            }
        }
    )


def remove_import(
    path: str, statement: str, *, expected_imports_ast_sha256: str
) -> Action:
    return Action(
        {
            "remove_import": {
                "path": path,
                "statement": statement,
                "expected_imports_ast_sha256": expected_imports_ast_sha256,
            }
        }
    )


def edit_imports(path: str, edits, *, expected_imports_ast_sha256: str) -> Action:
    """Apply ordered import edits guarded by the initial simulated import set."""
    return Action(
        {
            "edit_imports": {
                "path": path,
                "edits": edits,
                "expected_imports_ast_sha256": expected_imports_ast_sha256,
            }
        }
    )


__all__ = [
    "read_imports",
    "edit_imports",
    "ensure_import",
    "remove_import",
    "apply_diff",
    "create_directory",
    "create_file",
    "delete_exact",
    "delete_range",
    "environment",
    "file_info",
    "find_files",
    "git_status",
    "hash",
    "hash_range",
    "insert_after",
    "insert_at_line",
    "insert_before",
    "read",
    "read_symbol",
    "replace_exact",
    "replace_symbol",
    "move_file",
    "move_range",
    "move_symbol",
    "replace_range",
    "search",
    "search_context",
    "tree",
]
