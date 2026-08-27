"""Immutable declarative models for PatchShuttle jobs."""

from __future__ import annotations

import re
from collections.abc import Mapping
from enum import Enum
from typing import Annotated, Literal, TypeAlias, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    field_validator,
    model_validator,
)

from patchshuttle.identifiers import ProjectId

StrictString: TypeAlias = Annotated[str, Field(strict=True)]
NonEmptyString: TypeAlias = Annotated[str, Field(strict=True, min_length=1)]
PositiveInteger: TypeAlias = Annotated[int, Field(strict=True, ge=1)]
ContextLineCount: TypeAlias = Annotated[int, Field(strict=True, ge=0, le=500)]
Depth: TypeAlias = Annotated[int, Field(strict=True, ge=1, le=10)]
DiffStrip: TypeAlias = Annotated[int, Field(strict=True, ge=0, le=2)]
QuietLevel: TypeAlias = Annotated[int, Field(strict=True, ge=0, le=2)]
Sha256Hex: TypeAlias = Annotated[
    str,
    Field(strict=True, pattern=r"^[0-9A-Fa-f]{64}$"),
]
PythonSymbol: TypeAlias = Annotated[
    str,
    Field(
        strict=True,
        min_length=1,
        pattern=r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$",
    ),
]
NonEmptyStringTuple: TypeAlias = Annotated[
    tuple[NonEmptyString, ...], Field(min_length=1)
]
ModuleNameTuple: TypeAlias = Annotated[
    tuple[NonEmptyString, ...], Field(min_length=1, max_length=100)
]


class _FrozenModel(BaseModel):
    """Shared configuration for immutable models with a closed schema."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class TreeParameters(_FrozenModel):
    path: NonEmptyString = "."
    depth: Depth = 4
    max_entries: PositiveInteger = 500
    include_hidden: bool = Field(default=False, strict=True)


class ReadParameters(_FrozenModel):
    path: NonEmptyString
    start_line: PositiveInteger = 1
    end_line: PositiveInteger | None = None
    max_bytes: PositiveInteger | None = None

    @model_validator(mode="after")
    def validate_line_range(self) -> ReadParameters:
        if self.end_line is not None and self.end_line < self.start_line:
            raise ValueError("end_line must be greater than or equal to start_line")
        return self


class SearchParameters(_FrozenModel):
    path: NonEmptyString = "."
    text: NonEmptyString
    glob: StrictString | None = None
    case_sensitive: bool = Field(default=True, strict=True)
    max_results: PositiveInteger = 200


class SearchContextParameters(SearchParameters):
    before: ContextLineCount = 3
    after: ContextLineCount = 3


class ReadSymbolParameters(_FrozenModel):
    path: NonEmptyString
    symbol: PythonSymbol
    max_bytes: PositiveInteger | None = None


class PythonStructureParameters(_FrozenModel):
    path: NonEmptyString = "."
    max_files: PositiveInteger = 300
    max_symbols: PositiveInteger = 2000
    compact: bool = False


class FindFilesParameters(_FrozenModel):
    path: NonEmptyString = "."
    glob: NonEmptyString
    max_results: PositiveInteger = 500


class FileInfoParameters(_FrozenModel):
    path: NonEmptyString


class HashParameters(_FrozenModel):
    path: NonEmptyString
    algorithm: Literal["sha256"] = "sha256"


class _InclusiveLineRangeParameters(_FrozenModel):
    path: NonEmptyString
    start_line: PositiveInteger
    end_line: PositiveInteger

    @model_validator(mode="after")
    def validate_line_range(self) -> _InclusiveLineRangeParameters:
        if self.end_line < self.start_line:
            raise ValueError("end_line must be greater than or equal to start_line")
        return self


class HashRangeParameters(_InclusiveLineRangeParameters):
    algorithm: Literal["sha256"] = "sha256"


class GitStatusParameters(_FrozenModel):
    pass


class EnvironmentParameters(_FrozenModel):
    pass


class CreateDirectoryParameters(_FrozenModel):
    path: NonEmptyString


class CreateFileParameters(_FrozenModel):
    path: NonEmptyString
    content: StrictString
    encoding: NonEmptyString = "utf-8"
    newline: Literal["lf", "crlf"] = "lf"


class ReplaceExactParameters(_FrozenModel):
    path: NonEmptyString
    old: NonEmptyString
    new: StrictString
    expected_count: PositiveInteger = 1


class ReplaceSymbolParameters(_FrozenModel):
    path: NonEmptyString
    symbol: PythonSymbol
    expected_sha256: Sha256Hex
    new_content: StrictString

    @field_validator("expected_sha256")
    @classmethod
    def normalize_expected_sha256(cls, value: str) -> str:
        return value.casefold()


class InsertBeforeParameters(_FrozenModel):
    path: NonEmptyString
    anchor: NonEmptyString
    content: StrictString
    expected_count: PositiveInteger = 1


class InsertAfterParameters(_FrozenModel):
    path: NonEmptyString
    anchor: NonEmptyString
    content: StrictString
    expected_count: PositiveInteger = 1


class DeleteExactParameters(_FrozenModel):
    path: NonEmptyString
    text: NonEmptyString
    expected_count: PositiveInteger = 1


class _GuardedLineRangeParameters(_InclusiveLineRangeParameters):
    expected_content: NonEmptyString | None = None
    expected_sha256: Sha256Hex | None = None

    @field_validator("expected_sha256")
    @classmethod
    def normalize_expected_sha256(cls, value: str | None) -> str | None:
        return value.casefold() if value is not None else None

    @model_validator(mode="after")
    def require_guard(self) -> _GuardedLineRangeParameters:
        if self.expected_content is None and self.expected_sha256 is None:
            raise ValueError("at least one line-range guard is required")
        return self


class ReplaceRangeParameters(_GuardedLineRangeParameters):
    new_content: StrictString


class DeleteRangeParameters(_GuardedLineRangeParameters):
    pass


class InsertAtLineParameters(_FrozenModel):
    path: NonEmptyString
    line: PositiveInteger
    position: Literal["before", "after"]
    content: NonEmptyString
    expected_content: NonEmptyString | None = None
    expected_sha256: Sha256Hex | None = None

    @field_validator("expected_sha256")
    @classmethod
    def normalize_expected_sha256(cls, value: str | None) -> str | None:
        return value.casefold() if value is not None else None

    @model_validator(mode="after")
    def require_guard(self) -> InsertAtLineParameters:
        if self.expected_content is None and self.expected_sha256 is None:
            raise ValueError("at least one line guard is required")
        return self


class ApplyDiffParameters(_FrozenModel):
    diff: NonEmptyString
    strip: DiffStrip = 1


class CompileallParameters(_FrozenModel):
    paths: NonEmptyStringTuple
    quiet: QuietLevel = 1


class RuffParameters(_FrozenModel):
    pass


class PytestParameters(_FrozenModel):
    paths: tuple[NonEmptyString, ...] = ()
    args: tuple[StrictString, ...] = ()
    timeout_seconds: PositiveInteger | None = None


class UnittestParameters(_FrozenModel):
    discover: NonEmptyString
    pattern: NonEmptyString


class DjangoCheckParameters(_FrozenModel):
    manage_py: NonEmptyString


class DjangoMigrationsCheckParameters(_FrozenModel):
    manage_py: NonEmptyString


class DjangoTestParameters(_FrozenModel):
    manage_py: NonEmptyString
    labels: tuple[NonEmptyString, ...] = ()


class _ModuleImportParameters(_FrozenModel):
    modules: ModuleNameTuple

    @field_validator("modules")
    @classmethod
    def validate_module_names(cls, modules: tuple[str, ...]) -> tuple[str, ...]:
        invalid = [module for module in modules if not _MODULE_NAME.fullmatch(module)]
        if invalid:
            raise ValueError("modules must contain only dotted Python identifiers")
        if sum(len(module) for module in modules) > 8_000:
            raise ValueError("module names must not exceed 8000 characters in total")
        return modules


class ImportCheckParameters(_ModuleImportParameters):
    pass


class DjangoImportCheckParameters(_ModuleImportParameters):
    manage_py: NonEmptyString


class ProfileParameters(_FrozenModel):
    name: NonEmptyString


class TreeAction(_FrozenModel):
    tree: TreeParameters


class ReadAction(_FrozenModel):
    read: ReadParameters


class SearchAction(_FrozenModel):
    search: SearchParameters


class SearchContextAction(_FrozenModel):
    search_context: SearchContextParameters


class ReadSymbolAction(_FrozenModel):
    read_symbol: ReadSymbolParameters


class PythonStructureAction(_FrozenModel):
    python_structure: PythonStructureParameters


class FindFilesAction(_FrozenModel):
    find_files: FindFilesParameters


class FileInfoAction(_FrozenModel):
    file_info: FileInfoParameters


class HashAction(_FrozenModel):
    hash: HashParameters


class HashRangeAction(_FrozenModel):
    hash_range: HashRangeParameters


class GitStatusAction(_FrozenModel):
    git_status: GitStatusParameters


class EnvironmentAction(_FrozenModel):
    environment: EnvironmentParameters


class CreateDirectoryAction(_FrozenModel):
    create_directory: CreateDirectoryParameters


class CreateFileAction(_FrozenModel):
    create_file: CreateFileParameters


class ReplaceExactAction(_FrozenModel):
    replace_exact: ReplaceExactParameters


class ReplaceSymbolAction(_FrozenModel):
    replace_symbol: ReplaceSymbolParameters


class InsertBeforeAction(_FrozenModel):
    insert_before: InsertBeforeParameters


class InsertAfterAction(_FrozenModel):
    insert_after: InsertAfterParameters


class DeleteExactAction(_FrozenModel):
    delete_exact: DeleteExactParameters


class ReplaceRangeAction(_FrozenModel):
    replace_range: ReplaceRangeParameters


class DeleteRangeAction(_FrozenModel):
    delete_range: DeleteRangeParameters


class InsertAtLineAction(_FrozenModel):
    insert_at_line: InsertAtLineParameters


class ApplyDiffAction(_FrozenModel):
    apply_diff: ApplyDiffParameters


ActionValue: TypeAlias = (
    TreeAction
    | ReadAction
    | SearchAction
    | SearchContextAction
    | ReadSymbolAction
    | PythonStructureAction
    | FindFilesAction
    | FileInfoAction
    | HashAction
    | HashRangeAction
    | GitStatusAction
    | EnvironmentAction
    | CreateDirectoryAction
    | CreateFileAction
    | ReplaceExactAction
    | ReplaceSymbolAction
    | InsertBeforeAction
    | InsertAfterAction
    | DeleteExactAction
    | ReplaceRangeAction
    | DeleteRangeAction
    | InsertAtLineAction
    | ApplyDiffAction
)
ActionName: TypeAlias = Literal[
    "tree",
    "read",
    "search",
    "search_context",
    "read_symbol",
    "python_structure",
    "find_files",
    "file_info",
    "hash",
    "hash_range",
    "git_status",
    "environment",
    "create_directory",
    "create_file",
    "replace_exact",
    "replace_symbol",
    "insert_before",
    "insert_after",
    "delete_exact",
    "replace_range",
    "delete_range",
    "insert_at_line",
    "apply_diff",
]

_ACTION_MODELS: dict[str, type[_FrozenModel]] = {
    "tree": TreeAction,
    "read": ReadAction,
    "search": SearchAction,
    "search_context": SearchContextAction,
    "read_symbol": ReadSymbolAction,
    "python_structure": PythonStructureAction,
    "find_files": FindFilesAction,
    "file_info": FileInfoAction,
    "hash": HashAction,
    "hash_range": HashRangeAction,
    "git_status": GitStatusAction,
    "environment": EnvironmentAction,
    "create_directory": CreateDirectoryAction,
    "create_file": CreateFileAction,
    "replace_exact": ReplaceExactAction,
    "replace_symbol": ReplaceSymbolAction,
    "insert_before": InsertBeforeAction,
    "insert_after": InsertAfterAction,
    "delete_exact": DeleteExactAction,
    "replace_range": ReplaceRangeAction,
    "delete_range": DeleteRangeAction,
    "insert_at_line": InsertAtLineAction,
    "apply_diff": ApplyDiffAction,
}

AUDIT_ACTION_NAMES = frozenset(
    {
        "tree",
        "read",
        "search",
        "search_context",
        "read_symbol",
        "python_structure",
        "find_files",
        "file_info",
        "hash",
        "hash_range",
        "git_status",
        "environment",
    }
)
CHANGE_ACTION_NAMES = frozenset(
    {
        "create_directory",
        "create_file",
        "replace_exact",
        "replace_symbol",
        "insert_before",
        "insert_after",
        "delete_exact",
        "replace_range",
        "delete_range",
        "insert_at_line",
        "apply_diff",
    }
)


class Action(RootModel[ActionValue]):
    """One YAML-shaped action entry containing exactly one action name."""

    model_config = ConfigDict(frozen=True)

    @model_validator(mode="before")
    @classmethod
    def select_action_model(cls, value: object) -> object:
        if isinstance(value, Mapping):
            keys = list(value)
            if len(keys) != 1:
                raise ValueError("an action entry must contain exactly one action name")
            action_model = _ACTION_MODELS.get(keys[0])
            if action_model is None:
                raise ValueError(f"unknown action name {keys[0]!r}")
            return action_model.model_validate(value)
        return value

    @property
    def name(self) -> ActionName:
        return cast(ActionName, next(iter(type(self.root).model_fields)))

    @property
    def parameters(self) -> _FrozenModel:
        return cast(_FrozenModel, getattr(self.root, self.name))

    @property
    def is_audit(self) -> bool:
        return self.name in AUDIT_ACTION_NAMES

    @property
    def is_change(self) -> bool:
        return self.name in CHANGE_ACTION_NAMES


class CompileallCheck(_FrozenModel):
    compileall: CompileallParameters


class RuffCheck(_FrozenModel):
    ruff: RuffParameters


class PytestCheck(_FrozenModel):
    pytest: PytestParameters


class UnittestCheck(_FrozenModel):
    unittest: UnittestParameters


class DjangoCheck(_FrozenModel):
    django_check: DjangoCheckParameters


class DjangoMigrationsCheck(_FrozenModel):
    django_migrations_check: DjangoMigrationsCheckParameters


class DjangoTestCheck(_FrozenModel):
    django_test: DjangoTestParameters


class DjangoImportCheck(_FrozenModel):
    django_import_check: DjangoImportCheckParameters


class ImportCheck(_FrozenModel):
    import_check: ImportCheckParameters


class ProfileCheck(_FrozenModel):
    profile: ProfileParameters


CheckValue: TypeAlias = (
    CompileallCheck
    | RuffCheck
    | PytestCheck
    | UnittestCheck
    | DjangoCheck
    | DjangoMigrationsCheck
    | DjangoTestCheck
    | DjangoImportCheck
    | ImportCheck
    | ProfileCheck
)
CheckName: TypeAlias = Literal[
    "compileall",
    "ruff",
    "pytest",
    "unittest",
    "django_check",
    "django_migrations_check",
    "django_test",
    "django_import_check",
    "import_check",
    "profile",
]

_CHECK_MODELS: dict[str, type[_FrozenModel]] = {
    "compileall": CompileallCheck,
    "ruff": RuffCheck,
    "pytest": PytestCheck,
    "unittest": UnittestCheck,
    "django_check": DjangoCheck,
    "django_migrations_check": DjangoMigrationsCheck,
    "django_test": DjangoTestCheck,
    "django_import_check": DjangoImportCheck,
    "import_check": ImportCheck,
    "profile": ProfileCheck,
}


class Check(RootModel[CheckValue]):
    """One YAML-shaped check entry containing exactly one check name."""

    model_config = ConfigDict(frozen=True)

    @model_validator(mode="before")
    @classmethod
    def select_check_model(cls, value: object) -> object:
        if isinstance(value, Mapping):
            keys = list(value)
            if len(keys) != 1:
                raise ValueError("a check entry must contain exactly one check name")
            check_model = _CHECK_MODELS.get(keys[0])
            if check_model is None:
                raise ValueError(f"unknown check name {keys[0]!r}")
            return check_model.model_validate(value)
        return value

    @property
    def name(self) -> CheckName:
        return cast(CheckName, next(iter(type(self.root).model_fields)))

    @property
    def parameters(self) -> _FrozenModel:
        return cast(_FrozenModel, getattr(self.root, self.name))


class JobKind(str, Enum):
    """Supported v0.1 job kinds."""

    AUDIT = "audit"
    PATCH = "patch"
    VERIFY = "verify"


class Job(_FrozenModel):
    """One structurally validated PatchShuttle protocol v1 job."""

    protocol: Literal[1]
    project_id: ProjectId
    id: Annotated[str, Field(strict=True, pattern=r"^[A-Z][A-Z0-9_-]{2,63}$")]
    kind: JobKind
    title: StrictString | None = None
    description: StrictString | None = None
    actions: tuple[Action, ...] = ()
    checks: tuple[Check, ...] = ()

    @field_validator("protocol", mode="before")
    @classmethod
    def validate_protocol_is_strict_integer(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("protocol must be the integer 1")
        return value

    @model_validator(mode="after")
    def validate_kind_contract(self) -> Job:
        if self.kind is JobKind.AUDIT:
            if not self.actions:
                raise ValueError("audit jobs require at least one action")
            if any(not action.is_audit for action in self.actions):
                raise ValueError("audit jobs may contain only audit actions")
            if self.checks:
                raise ValueError("audit jobs may not contain checks")

        elif self.kind is JobKind.PATCH:
            if not self.actions:
                raise ValueError("patch jobs require at least one action")
            if any(not action.is_change for action in self.actions):
                raise ValueError("patch jobs may contain only change actions")

        else:
            if self.actions:
                raise ValueError("verify jobs may not contain actions")
            if not self.checks:
                raise ValueError("verify jobs require at least one check")

        return self


_MODULE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")

__all__ = ["Action", "ActionName", "Check", "CheckName", "Job", "JobKind"]
