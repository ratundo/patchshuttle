"""Declarative public check constructors."""

from __future__ import annotations

from collections.abc import Iterable

from patchshuttle.models import Check


def compileall(paths: Iterable[str], *, quiet: int = 1) -> Check:
    return Check({"compileall": {"paths": tuple(paths), "quiet": quiet}})


def ruff() -> Check:
    return Check({"ruff": {}})


def pytest(
    paths: Iterable[str] = (),
    *,
    args: Iterable[str] = (),
    timeout_seconds: int | None = None,
) -> Check:
    parameters = {"paths": tuple(paths), "args": tuple(args)}
    if timeout_seconds is not None:
        parameters["timeout_seconds"] = timeout_seconds
    return Check({"pytest": parameters})


def unittest(
    *,
    discover: str = "tests",
    pattern: str = "test_*.py",
) -> Check:
    return Check({"unittest": {"discover": discover, "pattern": pattern}})


def django_check(*, manage_py: str = "manage.py") -> Check:
    return Check({"django_check": {"manage_py": manage_py}})


def django_migrations_check(*, manage_py: str = "manage.py") -> Check:
    return Check({"django_migrations_check": {"manage_py": manage_py}})


def django_test(
    *,
    manage_py: str = "manage.py",
    labels: Iterable[str] = (),
) -> Check:
    return Check({"django_test": {"manage_py": manage_py, "labels": tuple(labels)}})


def django_import_check(
    modules: Iterable[str],
    *,
    manage_py: str = "manage.py",
) -> Check:
    return Check(
        {
            "django_import_check": {
                "manage_py": manage_py,
                "modules": tuple(modules),
            }
        }
    )


def import_check(modules: Iterable[str]) -> Check:
    return Check({"import_check": {"modules": tuple(modules)}})


def profile(name: str) -> Check:
    return Check({"profile": {"name": name}})


__all__ = [
    "compileall",
    "ruff",
    "django_check",
    "django_import_check",
    "django_migrations_check",
    "django_test",
    "import_check",
    "profile",
    "pytest",
    "unittest",
]
