"""Deterministic declaration-only Python structure extraction."""

from __future__ import annotations

import ast
import json
from dataclasses import asdict, dataclass
from typing import cast


@dataclass(frozen=True, slots=True)
class PythonImport:
    """One top-level import alias without imported code execution."""

    kind: str
    module: str | None
    name: str
    alias: str | None
    level: int


@dataclass(frozen=True, slots=True)
class PythonSymbol:
    """One lexical Python declaration and bounded structural metadata."""

    qualified_name: str
    kind: str
    lines: tuple[int, int]
    parameters: tuple[str, ...]
    decorators: tuple[str, ...]
    bases: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PythonFileStructure:
    """The declaration-only structure of one parsed Python file."""

    file: str
    imports: tuple[PythonImport, ...]
    symbols: tuple[PythonSymbol, ...]


def scan_python_structure(text: str, *, filename: str) -> PythonFileStructure:
    """Parse one file without importing or executing project code."""

    tree = ast.parse(text, filename=filename)
    visitor = _StructureVisitor()
    visitor.visit(tree)
    return PythonFileStructure(
        file=filename,
        imports=tuple(_module_imports(tree.body)),
        symbols=tuple(visitor.symbols),
    )


def render_python_structure(
    structure: PythonFileStructure,
    *,
    compact: bool = False,
) -> str:
    """Render one deterministic line-oriented structure record."""

    if compact:
        lines = [
            "schema: patchshuttle.python_structure.compact.v1",
            "file: " + json.dumps(structure.file, ensure_ascii=False),
            f"imports: {len(structure.imports)}",
            f"symbols: {len(structure.symbols)}",
        ]
        lines.extend(
            "symbol: "
            + json.dumps(
                {
                    "kind": item.kind,
                    "lines": item.lines,
                    "qualified_name": item.qualified_name,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            for item in structure.symbols
        )
        return "\n".join(lines)

    lines = [
        "schema: patchshuttle.python_structure.v1",
        "file: " + json.dumps(structure.file, ensure_ascii=False),
        f"imports: {len(structure.imports)}",
        f"symbols: {len(structure.symbols)}",
    ]
    lines.extend("import: " + _json_record(item) for item in structure.imports)
    lines.extend("symbol: " + _json_record(item) for item in structure.symbols)
    return "\n".join(lines)


class _StructureVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.scope: list[tuple[str, str]] = []
        self.symbols: list[PythonSymbol] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._visit_declaration(
            node,
            kind="class",
            parameters=(),
            bases=tuple(_expression_name(item) for item in node.bases),
        )

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        kind = "method" if self.scope and self.scope[-1][1] == "class" else "function"
        self._visit_declaration(
            node,
            kind=kind,
            parameters=_parameters(node.args),
            bases=(),
        )

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        kind = (
            "async_method"
            if self.scope and self.scope[-1][1] == "class"
            else "async_function"
        )
        self._visit_declaration(
            node,
            kind=kind,
            parameters=_parameters(node.args),
            bases=(),
        )

    def _visit_declaration(
        self,
        node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
        *,
        kind: str,
        parameters: tuple[str, ...],
        bases: tuple[str, ...],
    ) -> None:
        start_line = min(
            (node.lineno, *(decorator.lineno for decorator in node.decorator_list))
        )
        self.symbols.append(
            PythonSymbol(
                qualified_name=".".join((*[name for name, _ in self.scope], node.name)),
                kind=kind,
                lines=(start_line, cast(int, node.end_lineno)),
                parameters=parameters,
                decorators=tuple(
                    _expression_name(item) for item in node.decorator_list
                ),
                bases=bases,
            )
        )
        scope_kind = "class" if isinstance(node, ast.ClassDef) else "function"
        self.scope.append((node.name, scope_kind))
        self.generic_visit(node)
        self.scope.pop()


def _module_imports(body: list[ast.stmt]):
    for node in body:
        if isinstance(node, ast.Import):
            for item in node.names:
                yield PythonImport(
                    kind="import",
                    module=None,
                    name=item.name,
                    alias=item.asname,
                    level=0,
                )
        elif isinstance(node, ast.ImportFrom):
            for item in node.names:
                yield PythonImport(
                    kind="from_import",
                    module=node.module,
                    name=item.name,
                    alias=item.asname,
                    level=node.level,
                )


def _parameters(arguments: ast.arguments) -> tuple[str, ...]:
    values = [item.arg for item in (*arguments.posonlyargs, *arguments.args)]
    if arguments.vararg is not None:
        values.append("*" + arguments.vararg.arg)
    values.extend(item.arg for item in arguments.kwonlyargs)
    if arguments.kwarg is not None:
        values.append("**" + arguments.kwarg.arg)
    return tuple(values)


def _expression_name(value: ast.expr) -> str:
    if isinstance(value, ast.Name):
        return value.id
    if isinstance(value, ast.Attribute):
        return f"{_expression_name(value.value)}.{value.attr}"
    if isinstance(value, ast.Call):
        return _expression_name(value.func)
    if isinstance(value, ast.Subscript):
        return _expression_name(value.value)
    return f"<{type(value).__name__.casefold()}>"


def _json_record(value: PythonImport | PythonSymbol) -> str:
    return json.dumps(
        asdict(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


__all__ = [
    "PythonFileStructure",
    "PythonImport",
    "PythonSymbol",
    "render_python_structure",
    "scan_python_structure",
]
