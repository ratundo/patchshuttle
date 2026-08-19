"""Fixed djLint command construction from trusted local configuration."""

from __future__ import annotations

import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from patchshuttle.config import HtmlLintSettings


def djlint_argv(
    settings: HtmlLintSettings,
    source: str,
    *,
    stdin_filename: str | None = None,
) -> tuple[str, ...]:
    """Return one shell-free djLint argv for a file or stdin source."""

    argv = [
        sys.executable,
        "-I",
        "-m",
        "djlint",
        source,
        "--profile",
        settings.profile,
        "--lint",
    ]
    if stdin_filename is not None:
        argv.extend(("--stdin-filename", stdin_filename))
    if settings.ignore:
        argv.extend(("--ignore", ",".join(settings.ignore)))
    return tuple(argv)


@contextmanager
def isolated_djlint_directory() -> Iterator[Path]:
    """Yield a temporary root that prevents project djLint config discovery."""

    with tempfile.TemporaryDirectory(prefix="patchshuttle-djlint-") as raw:
        root = Path(raw)
        root.joinpath("djlint.toml").write_bytes(b"")
        yield root


__all__ = ["djlint_argv", "isolated_djlint_directory"]
