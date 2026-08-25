import hashlib

import pytest

from patchshuttle._python_symbols import find_symbol_source

SOURCE = (
    "def decorator(value):\n"
    "    return value\n"
    "\n"
    "@decorator\n"
    "class Service:\n"
    "    @decorator\n"
    "    async def method(self):\n"
    "        return None\n"
    "\n"
    "async def worker():\n"
    "    return None\n"
)


def test_find_symbol_source_returns_decorator_aware_canonical_ranges() -> None:
    service = find_symbol_source(SOURCE, "Service", filename="symbols.py")
    method = find_symbol_source(SOURCE, "Service.method", filename="symbols.py")
    worker = find_symbol_source(SOURCE, "worker", filename="symbols.py")

    assert service is not None
    assert service.kind == "class"
    assert (service.selected.start_line, service.selected.end_line) == (4, 8)
    assert service.selected.content.startswith("@decorator\nclass Service:\n")
    assert (
        service.selected.sha256
        == hashlib.sha256(service.selected.content.encode("utf-8")).hexdigest()
    )

    assert method is not None
    assert method.kind == "async_function"
    assert (method.selected.start_line, method.selected.end_line) == (6, 8)
    assert method.selected.content.startswith(
        "    @decorator\n    async def method(self):\n"
    )

    assert worker is not None
    assert worker.kind == "async_function"
    assert (worker.selected.start_line, worker.selected.end_line) == (10, 11)


def test_find_symbol_source_requires_one_exact_dotted_resolution() -> None:
    duplicated = "def task():\n    pass\n\ndef task():\n    pass\n"

    assert (
        find_symbol_source(
            duplicated,
            "task",
            filename="duplicate.py",
        )
        is None
    )
    assert find_symbol_source(SOURCE, "missing", filename="symbols.py") is None
    assert (
        find_symbol_source(
            SOURCE,
            "Service.missing",
            filename="symbols.py",
        )
        is None
    )


def test_find_symbol_source_propagates_python_syntax_errors() -> None:
    with pytest.raises(SyntaxError):
        find_symbol_source("def broken(:\n", "broken", filename="broken.py")
