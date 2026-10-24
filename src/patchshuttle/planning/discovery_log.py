"""Render discovery observations and explicitly advisory targeting guidance."""

import json

from patchshuttle._python_discovery import PythonDiscoveryEvaluation
from patchshuttle.planning.guidance import symbol_advisories


def _python_discovery_fields(
    evaluation: PythonDiscoveryEvaluation | None,
) -> str:
    from patchshuttle.logging import _fields, _json_paths

    if evaluation is None:
        return _fields(
            applicable="unknown",
            evidence_scope="CURRENT_JOB",
            evidence_status="UNAVAILABLE_UNPARSED_JOB",
            index_assessment="NOT_EVALUATED",
            explicit_python_paths="[]",
            python_read_actions=0,
            python_read_symbol_actions=0,
            python_structure_actions=0,
            python_search_actions=0,
            unclassified_search_actions=0,
            python_find_files_actions=0,
            python_files_reported=0,
            python_file_limit_reached="false",
            python_search_matches=0,
            python_searches_limited=0,
            python_audit_duration_ms=0,
            python_audit_output_bytes=0,
            python_audit_output_lines=0,
            declared_python_text_actions=0,
            declared_python_symbol_actions=0,
            declared_python_line_actions=0,
            declared_python_import_actions=0,
            declared_python_file_move_actions=0,
            failure_signal=None,
            reason_codes="[]",
            advisories="[]",
        )
    return _fields(
        applicable=str(evaluation.applicable).lower(),
        evidence_scope="CURRENT_JOB",
        evidence_status=("COLLECTED" if evaluation.applicable else "NOT_APPLICABLE"),
        index_assessment="NOT_EVALUATED",
        explicit_python_paths=_json_paths(evaluation.explicit_python_paths),
        python_read_actions=evaluation.python_read_actions,
        python_read_symbol_actions=evaluation.python_read_symbol_actions,
        python_structure_actions=evaluation.python_structure_actions,
        python_search_actions=evaluation.python_search_actions,
        unclassified_search_actions=evaluation.unclassified_search_actions,
        python_find_files_actions=evaluation.python_find_files_actions,
        python_files_reported=evaluation.python_files_reported,
        python_file_limit_reached=str(evaluation.python_file_limit_reached).lower(),
        python_search_matches=evaluation.python_search_matches,
        python_searches_limited=evaluation.python_searches_limited,
        python_audit_duration_ms=evaluation.python_audit_duration_ms,
        python_audit_output_bytes=evaluation.python_audit_output_bytes,
        python_audit_output_lines=evaluation.python_audit_output_lines,
        declared_python_text_actions=evaluation.declared_python_text_actions,
        declared_python_symbol_actions=evaluation.declared_python_symbol_actions,
        declared_python_line_actions=evaluation.declared_python_line_actions,
        declared_python_import_actions=evaluation.declared_python_import_actions,
        declared_python_file_move_actions=evaluation.declared_python_file_move_actions,
        failure_signal=evaluation.failure_signal,
        reason_codes=json.dumps(list(evaluation.reason_codes)),
        advisories=json.dumps(list(symbol_advisories(evaluation))),
    )
