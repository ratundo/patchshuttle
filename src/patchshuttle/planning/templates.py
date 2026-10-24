"""Reviewable audit-derived action fragments; never execute generated drafts."""

import json


def append_template(output: str, action: dict, maximum: int) -> str:
    """Include complete JSON (also valid YAML), or omit the entire fragment."""
    fragment = json.dumps([action], ensure_ascii=False, indent=2)
    combined = (
        output + "\naction_template_status: DRAFT_REVIEW_REQUIRED\n"
        "Use a separate patch job with project checks. Edit the draft before use.\n"
        "replace_symbol copies current content (no-op); edit_imports needs nonempty edits.\n"
        "```json\n" + fragment + "\n```"
    )
    if len(combined.encode("utf-8")) <= maximum:
        return combined
    return output + "\naction_template_status: OMITTED_MAX_BYTES"


def symbol_template(path, symbol, source):
    return {
        "replace_symbol": {
            "path": path,
            "symbol": symbol,
            "expected_sha256": source.selected.sha256,
            "expected_ast_sha256": source.ast_sha256,
            "new_content": source.selected.content,
        }
    }


def imports_template(path, digest):
    return {
        "edit_imports": {
            "path": path,
            "expected_imports_ast_sha256": digest,
            "edits": [],
        }
    }
