"""Immutable per-file formatter policy resolved during planning."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import PurePosixPath
from typing import Literal, TypeAlias

FormatterName: TypeAlias = Literal["isort", "black"]
FORMATTER_ORDER: tuple[FormatterName, FormatterName] = ("isort", "black")
BLACK_POLICY_OPTIONS = (
    "--no-cache",
    "--exclude",
    "",
    "--extend-exclude",
    "",
    "--force-exclude",
    "",
)


class FormatterDecision(str, Enum):
    """Whether local workspace policy allows one formatter to run."""

    RUN = "RUN"
    SKIP_LOCAL_POLICY = "SKIP_LOCAL_POLICY"


class FormatterCompatibility(str, Enum):
    """Read-only compatibility state for one source revision."""

    PASS = "PASS"
    INCOMPATIBLE = "INCOMPATIBLE"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    NOT_CHECKED = "NOT_CHECKED"


@dataclass(frozen=True, slots=True)
class PlannedFormatterTarget:
    """One formatter decision over one changed Python file."""

    formatter: FormatterName
    path: PurePosixPath
    decision: FormatterDecision
    baseline: FormatterCompatibility
    planned: FormatterCompatibility
    baseline_detail: str
    planned_detail: str


__all__ = [
    "FORMATTER_ORDER",
    "BLACK_POLICY_OPTIONS",
    "FormatterCompatibility",
    "FormatterDecision",
    "FormatterName",
    "PlannedFormatterTarget",
]
