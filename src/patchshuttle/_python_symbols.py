"""Deterministic Python symbol source discovery without source rewriting."""

from __future__ import annotations

import ast
from dataclasses import dataclass

from patchshuttle._line_ranges import CanonicalLineRange, select_line_range


@dataclass(frozen=True, slots=True)
class PythonSymbolSource:
    """One exactly resolved Python symbol and its canonical source selection."""

    kind: str
    selected: CanonicalLineRange


def find_symbol_source(
    text: str,
    symbol: str,
    *,
    filename: str,
) -> PythonSymbolSource | None:
    """Return one exact dotted Python symbol, or None when resolution is not unique."""

    current: ast.AST = ast.parse(text, filename=filename)
    for part in symbol.split("."):
        body = getattr(current, "body", ())
        matches = [
            item
            for item in body
            if isinstance(
                item,
                (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
            )
            and item.name == part
        ]
        if len(matches) != 1:
            return None
        current = matches[0]

    if not isinstance(
        current,
        (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
    ):
        return None
    decorators = current.decorator_list
    start_line = min((current.lineno, *(item.lineno for item in decorators)))
    end_line = current.end_lineno
    if end_line is None:  # pragma: no cover - Python 3.10+ AST contract
        raise ValueError("Python symbol does not expose an end line")
    selected = select_line_range(
        text,
        start_line=start_line,
        end_line=end_line,
    )
    kind = {
        ast.ClassDef: "class",
        ast.FunctionDef: "function",
        ast.AsyncFunctionDef: "async_function",
    }[type(current)]
    return PythonSymbolSource(kind=kind, selected=selected)
