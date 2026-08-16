"""Require a release tag to match the source package version."""

from __future__ import annotations

import argparse
import re
from pathlib import Path


def source_version(root: Path) -> str:
    source = (root / "src/patchshuttle/_version.py").read_text("utf-8")
    match = re.search(r'^__version__ = "([^"]+)"$', source, flags=re.MULTILINE)
    if match is None:
        raise ValueError("source version file has an unexpected format")
    return match.group(1)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("tag")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    arguments = parser.parse_args()
    try:
        version = source_version(arguments.root.resolve())
    except (OSError, ValueError) as exc:
        parser.exit(1, f"TAG_CHECK_FAILED: {exc}\n")
    expected = f"v{version}"
    if arguments.tag != expected:
        parser.exit(
            1,
            f"TAG_CHECK_FAILED: expected {expected}, received {arguments.tag}\n",
        )
    print(f"TAG_CHECK_OK {arguments.tag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
