"""Tests for declaration-only Python structure extraction."""

import ast
import json

import pytest

from patchshuttle._python_structure import (
    PythonFileStructure,
    PythonImport,
    PythonSymbol,
    render_python_structure,
    scan_python_structure,
)


def test_scan_collects_imports_and_lexical_declarations_without_literals() -> None:
    source = """\
import os as operating_system, sys
from .pkg import value as alias, other
from . import local

@registry.route("PRIVATE_ROUTE")
class Service(Base, Generic["PRIVATE_TYPE"]):
    @cached
    def run(self, item):
        def inner(value=PRIVATE_DEFAULT):
            return value
        return inner(item)

    async def stream(self, /, item, *args, flag=True, **kwargs):
        return item

async def worker():
    return None

def plain(item):
    class Nested:
        def method(self):
            return item
    return Nested
"""

    structure = scan_python_structure(source, filename="pkg/service.py")

    assert structure.file == "pkg/service.py"
    assert structure.imports == (
        PythonImport("import", None, "os", "operating_system", 0),
        PythonImport("import", None, "sys", None, 0),
        PythonImport("from_import", "pkg", "value", "alias", 1),
        PythonImport("from_import", "pkg", "other", None, 1),
        PythonImport("from_import", None, "local", None, 1),
    )
    symbols = {item.qualified_name: item for item in structure.symbols}
    assert tuple(symbols) == (
        "Service",
        "Service.run",
        "Service.run.inner",
        "Service.stream",
        "worker",
        "plain",
        "plain.Nested",
        "plain.Nested.method",
    )
    assert symbols["Service"] == PythonSymbol(
        qualified_name="Service",
        kind="class",
        lines=(5, 14),
        parameters=(),
        decorators=("registry.route",),
        bases=("Base", "Generic"),
    )
    assert symbols["Service.run"].kind == "method"
    assert symbols["Service.run"].parameters == ("self", "item")
    assert symbols["Service.run"].decorators == ("cached",)
    assert symbols["Service.run.inner"].kind == "function"
    assert symbols["Service.stream"].kind == "async_method"
    assert symbols["Service.stream"].parameters == (
        "self",
        "item",
        "*args",
        "flag",
        "**kwargs",
    )
    assert symbols["worker"].kind == "async_function"
    assert symbols["worker"].parameters == ()
    assert symbols["plain"].kind == "function"
    assert symbols["plain.Nested.method"].kind == "method"

    rendered = render_python_structure(structure)
    assert "PRIVATE_ROUTE" not in rendered
    assert "PRIVATE_TYPE" not in rendered
    assert "PRIVATE_DEFAULT" not in rendered


def test_expression_names_are_bounded_and_do_not_render_values() -> None:
    structure = scan_python_structure(
        """\
@factory["PRIVATE_SUBSCRIPT"]
def selected():
    pass

@42
def unusual():
    pass

class Derived(builder().Base):
    pass
""",
        filename="expressions.py",
    )

    selected, unusual, derived = structure.symbols
    assert selected.decorators == ("factory",)
    assert unusual.decorators == ("<constant>",)
    assert derived.bases == ("builder.Base",)
    rendered = render_python_structure(structure)
    assert "PRIVATE_SUBSCRIPT" not in rendered


def test_render_is_deterministic_line_oriented_json() -> None:
    structure = PythonFileStructure(
        file="модуль.py",
        imports=(PythonImport("import", None, "json", None, 0),),
        symbols=(
            PythonSymbol(
                qualified_name="run",
                kind="function",
                lines=(2, 3),
                parameters=("value",),
                decorators=(),
                bases=(),
            ),
        ),
    )

    output = render_python_structure(structure)
    lines = output.splitlines()

    assert lines[:4] == [
        "schema: patchshuttle.python_structure.v1",
        'file: "модуль.py"',
        "imports: 1",
        "symbols: 1",
    ]
    assert json.loads(lines[4].removeprefix("import: ")) == {
        "alias": None,
        "kind": "import",
        "level": 0,
        "module": None,
        "name": "json",
    }
    assert json.loads(lines[5].removeprefix("symbol: "))["lines"] == [2, 3]

    compact = render_python_structure(structure, compact=True)
    compact_lines = compact.splitlines()

    assert compact_lines[:4] == [
        "schema: patchshuttle.python_structure.compact.v1",
        'file: "модуль.py"',
        "imports: 1",
        "symbols: 1",
    ]
    assert all(not line.startswith("import: ") for line in compact_lines)
    assert json.loads(compact_lines[4].removeprefix("symbol: ")) == {
        "kind": "function",
        "lines": [2, 3],
        "qualified_name": "run",
    }
    assert len(compact.encode("utf-8")) < len(output.encode("utf-8"))


def test_empty_file_and_invalid_syntax_are_explicit() -> None:
    empty = scan_python_structure("", filename="empty.py")

    assert empty.imports == ()
    assert empty.symbols == ()
    assert render_python_structure(empty).splitlines()[-2:] == [
        "imports: 0",
        "symbols: 0",
    ]
    with pytest.raises(SyntaxError):
        scan_python_structure("def broken(:\n", filename="broken.py")


def test_end_line_contract_uses_ast_location() -> None:
    tree = ast.parse("def run():\n    pass\n")
    node = tree.body[0]

    assert isinstance(node, ast.FunctionDef)
    assert node.end_lineno == 2
    assert scan_python_structure("def run():\n    pass\n", filename="one.py").symbols[
        0
    ].lines == (1, 2)
