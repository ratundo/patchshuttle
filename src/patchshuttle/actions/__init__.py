"""Guarded low-level actions used by the transaction runner."""

from patchshuttle.actions.constructors import (
    apply_diff,
)
from patchshuttle.actions.constructors import (
    create_directory as _declarative_create_directory,
)
from patchshuttle.actions.constructors import (
    create_file,
    delete_exact,
    delete_range,
    environment,
    file_info,
    find_files,
    git_status,
    hash,
    hash_range,
    insert_after,
    insert_at_line,
    insert_before,
    read,
    replace_exact,
    replace_range,
    search,
    tree,
)
from patchshuttle.actions.create import (
    FilePublishError,
    atomic_create_file,
)
from patchshuttle.actions.create import create_directory as apply_create_directory
from patchshuttle.actions.create import (
    verify_created_directory,
    verify_created_file,
)
from patchshuttle.actions.modify import (
    FileReplaceError,
    atomic_replace_file,
    atomic_restore_file,
    verify_modified_file,
    verify_restored_file,
)
from patchshuttle.workspace import Workspace


def create_directory(*args, **kwargs):
    """Create a declarative action, retaining the legacy internal call form."""

    if (args and isinstance(args[0], Workspace)) or isinstance(
        kwargs.get("workspace"),
        Workspace,
    ):
        return apply_create_directory(*args, **kwargs)
    return _declarative_create_directory(*args, **kwargs)


__all__ = [
    "FilePublishError",
    "FileReplaceError",
    "atomic_create_file",
    "atomic_replace_file",
    "atomic_restore_file",
    "apply_create_directory",
    "apply_diff",
    "create_directory",
    "create_file",
    "delete_exact",
    "delete_range",
    "environment",
    "file_info",
    "find_files",
    "git_status",
    "hash",
    "hash_range",
    "insert_after",
    "insert_at_line",
    "insert_before",
    "read",
    "replace_exact",
    "replace_range",
    "search",
    "tree",
    "verify_created_directory",
    "verify_created_file",
    "verify_modified_file",
    "verify_restored_file",
]
