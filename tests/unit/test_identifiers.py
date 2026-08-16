"""Contract tests for PatchShuttle project identifiers."""

import re

import patchshuttle.identifiers as identifiers_module
from patchshuttle.identifiers import generate_project_id


def test_project_id_uses_eight_cryptographically_secure_random_bytes(
    monkeypatch,
) -> None:
    calls: list[int] = []

    def fake_token_hex(byte_count: int) -> str:
        calls.append(byte_count)
        return "8f41c2a73d905e61"

    monkeypatch.setattr(identifiers_module.secrets, "token_hex", fake_token_hex)

    assert generate_project_id() == "PSH-8F41C2A73D905E61"
    assert calls == [8]


def test_generated_project_id_has_the_canonical_format() -> None:
    assert re.fullmatch(r"PSH-[0-9A-F]{16}", generate_project_id())
