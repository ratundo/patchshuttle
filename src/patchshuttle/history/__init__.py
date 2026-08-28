"""Structured append-only PatchShuttle execution history."""

from patchshuttle.history.models import (
    HISTORY_SCHEMA,
    HISTORY_SCHEMA_VERSION,
    HistoryError,
    HistoryErrorCode,
    HistoryListResult,
    HistoryRecord,
    HistoryWriteResult,
)
from patchshuttle.history.records import build_history_record
from patchshuttle.history.storage import (
    latest_history_record,
    list_history_records,
    read_history_record,
    try_write_history_record,
    write_history_record,
)

__all__ = [
    "HISTORY_SCHEMA",
    "HISTORY_SCHEMA_VERSION",
    "HistoryError",
    "HistoryErrorCode",
    "HistoryListResult",
    "HistoryRecord",
    "HistoryWriteResult",
    "build_history_record",
    "latest_history_record",
    "list_history_records",
    "read_history_record",
    "try_write_history_record",
    "write_history_record",
]
