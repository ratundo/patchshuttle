"""Canonical project identifiers for PatchShuttle workspaces and jobs."""

from __future__ import annotations

import secrets
from typing import Annotated, TypeAlias

from pydantic import Field

PROJECT_ID_PATTERN = r"^PSH-[0-9A-F]{16}$"
ProjectId: TypeAlias = Annotated[str, Field(strict=True, pattern=PROJECT_ID_PATTERN)]


def generate_project_id() -> str:
    """Return a project identifier backed by eight secure random bytes."""

    return f"PSH-{secrets.token_hex(8).upper()}"


__all__ = ["PROJECT_ID_PATTERN", "ProjectId", "generate_project_id"]
