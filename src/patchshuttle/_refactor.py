"""Pure source transfers; no filesystem access or project-code execution."""

from __future__ import annotations

import ast
import io
import tokenize

from patchshuttle._line_ranges import select_line_range
from patchshuttle._python_symbols import find_symbol_source


def move_range(source: str, destination: str, *, start: int, end: int, line: int):
    """Transfer physical lines before a destination line (or EOF + 1)."""
    selected = select_line_range(source, start_line=start, end_line=end)
    lines = destination.splitlines(keepends=True)
    if not 1 <= line <= len(lines) + 1:
        raise ValueError("destination_line must be between 1 and line count + 1")
    offset = sum(map(len, lines[: line - 1]))
    if offset and destination[offset - 1] != "\n":
        raise ValueError("destination insertion requires a preceding newline")
    if destination[offset:] and not selected.content.endswith("\n"):
        raise ValueError("moved range needs a final newline before destination content")
    return (
        source[: selected.start_offset] + source[selected.end_offset :],
        destination[:offset] + selected.content + destination[offset:],
    )


def _resolve(tree: ast.AST, symbol: str):
    parent = None
    current = tree
    for part in symbol.split("."):
        matches = [
            n
            for n in getattr(current, "body", ())
            if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name == part
        ]
        if len(matches) != 1:
            raise ValueError("symbol must resolve exactly once")
        parent, current = current, matches[0]
    return parent, current


def _reindent(content: str, old: str, new: str) -> str:
    if old == new:
        return content
    for token in tokenize.generate_tokens(io.StringIO(content).readline):
        if token.type == tokenize.STRING and token.start[0] != token.end[0]:
            raise ValueError("reindent of multiline string literals is not supported")
    lines = content.splitlines(keepends=True)
    if any(line.strip() and not line.startswith(old) for line in lines):
        raise ValueError("symbol uses inconsistent indentation")
    return "".join(new + line[len(old) :] if line.strip() else line for line in lines)


def move_symbol(
    source: str, destination: str, *, symbol: str, destination_class: str | None
):
    """Move a module declaration or direct class member, retaining its source."""
    tree = ast.parse(source)
    parent, node = _resolve(tree, symbol)
    if not isinstance(parent, (ast.Module, ast.ClassDef)):
        raise ValueError("moving closure-bound nested declarations is not supported")
    target_tree = ast.parse(destination)
    target = target_tree
    if destination_class is not None:
        _, target = _resolve(target_tree, destination_class)
        if not isinstance(target, ast.ClassDef):
            raise ValueError("destination_class must resolve to a class")
    if any(
        isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name == node.name
        for n in target.body
    ):
        raise ValueError("destination already contains that declaration")
    for child in ast.walk(target):
        if (
            isinstance(child, ast.Name)
            and isinstance(child.ctx, ast.Store)
            and child.id == node.name
        ) or (
            isinstance(child, ast.alias)
            and (child.asname or child.name.split(".")[0]) == node.name
        ):
            raise ValueError("destination already binds that name")
    # These bindings change when the lexical class changes. Fail closed.
    if isinstance(parent, ast.ClassDef) or isinstance(target, ast.ClassDef):
        if node.name.startswith("__") and not node.name.endswith("__"):
            raise ValueError(
                "class-bound private declaration requires an explicit rewrite"
            )
        for child in ast.walk(node):
            if (
                isinstance(child, ast.Name)
                and (
                    child.id in {"super", "__class__"}
                    or (child.id.startswith("__") and not child.id.endswith("__"))
                )
            ) or (
                isinstance(child, ast.Attribute)
                and child.attr.startswith("__")
                and not child.attr.endswith("__")
            ):
                raise ValueError(
                    "class-bound super, __class__ or private names require an explicit rewrite"
                )
    selected = find_symbol_source(source, symbol, filename="source.py").selected
    first = selected.content.splitlines()[0]
    old_indent = first[: len(first) - len(first.lstrip())]
    new_indent = ""
    offset = len(destination)
    if isinstance(target, ast.ClassDef):
        first_member = target.body[0]
        member_line = destination.splitlines()[first_member.lineno - 1]
        new_indent = member_line[: len(member_line) - len(member_line.lstrip())]
        if first_member.lineno == target.lineno:
            raise ValueError("one-line destination classes must be expanded first")
        offset = sum(
            map(len, destination.splitlines(keepends=True)[: target.end_lineno])
        )
    moved = _reindent(selected.content, old_indent, new_indent)
    if not moved.endswith("\n"):
        moved += "\n"
    prefix = destination[:offset]
    if prefix and not prefix.endswith("\n"):
        prefix += "\n"
    updated_destination = prefix + moved + destination[offset:]
    filler = (
        old_indent + "pass\n"
        if isinstance(parent, ast.ClassDef) and len(parent.body) == 1
        else ""
    )
    updated_source = (
        source[: selected.start_offset] + filler + source[selected.end_offset :]
    )
    compile(updated_source, "source.py", "exec", dont_inherit=True)
    compile(updated_destination, "destination.py", "exec", dont_inherit=True)
    target_name = f"{destination_class}.{node.name}" if destination_class else node.name
    moved_source = find_symbol_source(
        updated_destination, target_name, filename="destination.py"
    )
    original = find_symbol_source(source, symbol, filename="source.py")
    if moved_source is None or moved_source.ast_sha256 != original.ast_sha256:
        raise ValueError("transfer changed the declaration's AST")
    return updated_source, updated_destination
