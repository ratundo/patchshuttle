"""Validate PatchShuttle release distributions and write their checksums."""

from __future__ import annotations

import argparse
import hashlib
import re
import tarfile
import zipfile
from email.parser import BytesParser
from email.policy import compat32
from pathlib import Path

_NAME = "patchshuttle"
_METADATA_VERSION = "2.4"


def _source_version(root: Path) -> str:
    source = (root / "src/patchshuttle/_version.py").read_text("utf-8")
    match = re.search(r'^__version__ = "([^"]+)"$', source, flags=re.MULTILINE)
    if match is None:
        raise ValueError("source version file has an unexpected format")
    return match.group(1)


def _artifacts(dist: Path, version: str) -> tuple[Path, Path]:
    distributions = sorted(
        path
        for path in dist.iterdir()
        if path.is_file() and (path.suffix == ".whl" or path.name.endswith(".tar.gz"))
    )
    expected = {
        f"{_NAME}-{version}-py3-none-any.whl",
        f"{_NAME}-{version}.tar.gz",
    }
    actual = {path.name for path in distributions}
    if actual != expected:
        raise ValueError(
            "dist must contain exactly the expected wheel and sdist: "
            + ", ".join(sorted(expected))
        )
    wheel = dist / f"{_NAME}-{version}-py3-none-any.whl"
    sdist = dist / f"{_NAME}-{version}.tar.gz"
    return wheel, sdist


def _parse_metadata(raw: bytes, *, label: str):
    metadata = BytesParser(policy=compat32).parsebytes(raw)
    expected = {
        "Metadata-Version": _METADATA_VERSION,
        "Name": _NAME,
    }
    for field, value in expected.items():
        if metadata[field] != value:
            raise ValueError(f"{label} has unexpected {field}: {metadata[field]!r}")
    extras = metadata.get_all("Provides-Extra") or []
    if "html" not in extras:
        raise ValueError(f"{label} does not declare the html extra")
    requirements = metadata.get_all("Requires-Dist") or []
    html_requirements = [
        value.casefold()
        for value in requirements
        if value.casefold().startswith("djlint")
    ]
    if not html_requirements or not any(
        'extra == "html"' in value or "extra == 'html'" in value
        for value in html_requirements
    ):
        raise ValueError(f"{label} does not bind djLint to the html extra")
    return metadata


def _validate_wheel(wheel: Path, version: str) -> None:
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        metadata_names = [
            name for name in names if name.endswith(".dist-info/METADATA")
        ]
        if len(metadata_names) != 1:
            raise ValueError("wheel must contain exactly one METADATA file")
        metadata = _parse_metadata(
            archive.read(metadata_names[0]),
            label="wheel",
        )
        if metadata["Version"] != version:
            raise ValueError("wheel version does not match the source version")
        required = {
            "patchshuttle/py.typed",
            "patchshuttle/resources/AI_GUIDE.md",
            "patchshuttle/resources/PATCHSHUTTLE_PROTOCOL.md",
        }
        missing = sorted(required.difference(names))
        if missing:
            raise ValueError("wheel is missing required files: " + ", ".join(missing))
        prohibited = [
            name
            for name in names
            if name.startswith("tests/")
            or "/__pycache__/" in name
            or name.endswith((".pyc", ".pyo"))
        ]
        if prohibited:
            raise ValueError("wheel contains development artifacts")


def _validate_sdist(sdist: Path, version: str) -> None:
    prefix = f"{_NAME}-{version}/"
    required = {
        prefix + ".github/workflows/ci.yml",
        prefix + ".github/workflows/release.yml",
        prefix + ".github/workflows/testpypi.yml",
        prefix + "CHANGELOG.md",
        prefix + "LICENSE",
        prefix + "README.md",
        prefix + "SECURITY.md",
        prefix + "SPEC_V0_1.md",
        prefix + "docs/RELEASE.md",
        prefix + "pyproject.toml",
        prefix + "tools/wheel_smoke.py",
    }
    with tarfile.open(sdist, mode="r:gz") as archive:
        names = {member.name for member in archive.getmembers()}
        missing = sorted(required.difference(names))
        if missing:
            raise ValueError("sdist is missing required files: " + ", ".join(missing))
        metadata_member = archive.getmember(prefix + "PKG-INFO")
        extracted = archive.extractfile(metadata_member)
        if extracted is None:
            raise ValueError("sdist PKG-INFO is unavailable")
        metadata = _parse_metadata(extracted.read(), label="sdist")
        if metadata["Version"] != version:
            raise ValueError("sdist version does not match the source version")
        prohibited = [
            name
            for name in names
            if "/__pycache__/" in name or name.endswith((".pyc", ".pyo"))
        ]
        if prohibited:
            raise ValueError("sdist contains Python cache artifacts")


def _write_checksums(dist: Path, artifacts: tuple[Path, Path]) -> Path:
    lines = [
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}"
        for path in sorted(artifacts, key=lambda item: item.name)
    ]
    target = dist / "SHA256SUMS"
    temporary = dist / ".SHA256SUMS.tmp"
    temporary.write_text("\n".join(lines) + "\n", encoding="ascii", newline="\n")
    temporary.replace(target)
    return target


def validate_release(root: Path, dist: Path) -> tuple[str, Path]:
    """Validate exactly one source/wheel pair and return its version/checksums."""

    version = _source_version(root)
    wheel, sdist = _artifacts(dist, version)
    _validate_wheel(wheel, version)
    _validate_sdist(sdist, version)
    checksums = _write_checksums(dist, (wheel, sdist))
    return version, checksums


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("dist", type=Path)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    arguments = parser.parse_args()
    try:
        version, checksums = validate_release(
            arguments.root.resolve(),
            arguments.dist.resolve(),
        )
    except (OSError, ValueError, tarfile.TarError, zipfile.BadZipFile) as exc:
        parser.exit(1, f"RELEASE_CHECK_FAILED: {exc}\n")
    print(f"RELEASE_CHECK_OK {version}")
    print(checksums)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
