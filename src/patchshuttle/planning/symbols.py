"""Structural replacement validation and actionable, bounded failure context."""

import ast
import textwrap

from patchshuttle._python_symbols import find_symbol_source


def validate_replacement(updated, replacement, symbol, kind, filename):
    """Keep the existing structural guard contract without rewriting input."""
    replacement_tree = ast.parse(textwrap.dedent(replacement))
    expected_type = {
        "class": ast.ClassDef,
        "function": ast.FunctionDef,
        "async_function": ast.AsyncFunctionDef,
    }[kind]
    if (
        len(replacement_tree.body) != 1
        or not isinstance(replacement_tree.body[0], expected_type)
        or replacement_tree.body[0].name != symbol.split(".")[-1]
    ):
        raise ValueError(
            "replacement must contain exactly the same symbol name and kind"
        )
    compile(updated, filename, "exec", dont_inherit=True)
    resolved = find_symbol_source(updated, symbol, filename=filename)
    if resolved is None or resolved.kind != kind:
        raise ValueError("replacement must preserve unique symbol resolution")


def _indent(text):
    line = next(
        (
            line
            for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ),
        "",
    )
    prefix = line[: len(line) - len(line.lstrip(" \t\f"))]
    return f"{prefix[:80]!r} (characters: {len(prefix)}; preview limit: 80)"


def replacement_diagnostics(original, replacement):
    """Describe first code-line prefixes; these do not diagnose every syntax error."""
    return (
        f"  source_first_code_indent: {_indent(original)}",
        f"  replacement_first_code_indent: {_indent(replacement)}",
        "  hint: Keep destination indentation from read_symbol, including decorators and body.",
        "  hint: YAML block indentation is removed before new_content is used; retain Python indentation inside it.",
        "  hint: Fix syntax/name/kind errors shown above; indentation context alone does not identify their cause.",
        "  hint: No automatic reindentation is performed; preserve multiline-string contents.",
    )
