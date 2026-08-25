"""Workspace-independent capability, schema, and operation documentation."""

from __future__ import annotations

import json

from patchshuttle._version import __version__
from patchshuttle.models import Job

JOB_KINDS = ("audit", "patch", "verify")
AUDIT_ACTIONS = (
    "tree",
    "read",
    "search",
    "search_context",
    "read_symbol",
    "find_files",
    "file_info",
    "hash",
    "hash_range",
    "git_status",
    "environment",
)
CHANGE_ACTIONS = (
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
)
CHECKS = (
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
)
FORMATTERS = ("isort", "black")

_EXPLANATIONS = {
    "create_directory": """\
category: change_action
summary: Create one missing directory inside the workspace.
fields: path (required relative path)
planning: Existing directories become NO_CHANGE; conflicting files are rejected.
safety: Protected, ignored, absolute, traversing, and symlinked paths are rejected.""",
    "create_file": """\
category: change_action
summary: Create one text file or accept identical existing content as NO_CHANGE.
fields: path and content (required); encoding=utf-8 and newline=lf (optional)
planning: Different existing content is rejected instead of overwritten.
safety: Content is bounded and binary content is rejected.""",
    "replace_exact": """\
category: change_action
summary: Replace an exact text occurrence count in one existing text file.
fields: path, old, and new (required); expected_count=1 (optional)
planning: The exact old text must occur expected_count times in the current simulated file.
diagnostics: A mismatch reports exact match lines or up to three bounded similarity-ranked nearby snippets.
whitespace: Exact means exact; indentation, line endings, and spaces are not normalized.
example:
  - replace_exact:
      path: src/example.py
      old: "VALUE = 1"
      new: "VALUE = 2"
      expected_count: 1""",
    "replace_symbol": """\
category: change_action
summary: Replace one exactly resolved decorator-aware Python symbol.
fields: path, dotted symbol, expected_sha256, and new_content (required)
planning: Resolution and guard validation use the current sequential simulated file state.
canonicalization: The guard is read_symbol SHA-256 over canonical LF UTF-8 symbol source.
idempotency: Exact desired symbol content becomes NO_CHANGE even when the prior guard no longer matches.
safety: Syntax, missing or duplicate resolution, and hash mismatches fail without fuzzy relocation or line shifting.
example:
  - replace_symbol:
      path: src/example.py
      symbol: Service.run
      expected_sha256: "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
      new_content: |
            def run(self):
                return 2""",
    "insert_before": """\
category: change_action
summary: Insert content immediately before an exact anchor.
fields: path, anchor, and content (required); expected_count=1 (optional)
planning: The anchor must occur exactly expected_count times in the simulated file.
diagnostics: A mismatch reports exact match lines or bounded nearby snippets.""",
    "insert_after": """\
category: change_action
summary: Insert content immediately after an exact anchor.
fields: path, anchor, and content (required); expected_count=1 (optional)
planning: The anchor must occur exactly expected_count times in the simulated file.
diagnostics: A mismatch reports exact match lines or bounded nearby snippets.""",
    "delete_exact": """\
category: change_action
summary: Delete an exact text occurrence count from one existing text file.
fields: path and text (required); expected_count=1 (optional)
planning: The text must occur exactly expected_count times in the simulated file.
diagnostics: A mismatch reports exact match lines or bounded nearby snippets.""",
    "replace_range": """\
category: change_action
summary: Replace one 1-based inclusive physical-line range after strict guard validation.
fields: path, start_line, end_line, and new_content (required); expected_content and/or expected_sha256 (at least one required)
planning: Line numbers position the range; guards prove the current simulated content before any change.
canonicalization: Range content uses LF newlines and SHA-256 over canonical UTF-8 bytes.
safety: Both guards must pass when both are supplied; no fuzzy matching, relocation, partial apply, or automatic line shift.""",
    "delete_range": """\
category: change_action
summary: Delete one 1-based inclusive physical-line range after strict guard validation.
fields: path, start_line, and end_line (required); expected_content and/or expected_sha256 (at least one required)
planning: Guards are evaluated against the sequential simulated file state.
safety: A bounds or guard mismatch stops planning without changing the workspace.""",
    "insert_at_line": """\
category: change_action
summary: Insert content before or after one guarded physical line.
fields: path, line, position=before|after, and content (required); expected_content and/or expected_sha256 (at least one required)
planning: The referenced line is guarded before insertion and positions are 1-based.
safety: Line numbers are never accepted as proof of identity.""",
    "search_context": """\
category: audit_action
summary: Find literal text and return bounded physical-line context for each match.
fields: text (required); path=., glob=null, case_sensitive=true, max_results=200, before=3, and after=3 (optional)
output: Each deterministic match includes its path, line, bounded range, and numbered source lines.
safety: Read-only, literal rather than regex, bounded, and subject to workspace audit policy.""",
    "read_symbol": """\
category: audit_action
summary: Read one exactly resolved Python class, function, method, or nested symbol.
fields: path and dotted symbol (required); max_bytes (optional)
output: Includes symbol kind, decorator-aware physical range, canonical SHA-256, and numbered source lines.
safety: Python AST locates boundaries without rewriting source; missing or duplicate symbols fail exactly.""",
    "hash_range": """\
category: audit_action
summary: Calculate the canonical SHA-256 guard for one 1-based inclusive physical-line range.
fields: path, start_line, and end_line (required); algorithm=sha256 (optional)
canonicalization: Newlines are LF and canonical text is encoded as UTF-8 before hashing.
output: Includes source encoding, requested bounds, total lines, final-newline state, digest, and canonical byte size.
safety: Read-only, bounded, and subject to workspace audit policy.""",
    "apply_diff": """\
category: change_action
summary: Apply a text-only unified diff in memory without a shell or patch executable.
fields: diff (required); strip=1 (optional, allowed values 0 through 2)
planning: File headers, hunk counts, paths, context, additions, and removals are validated.
diagnostics: Failures report hunk numbers, declared and actual counts, and the first context mismatch.
safety: Binary diffs, protected paths, traversal, and unsupported file states are rejected.""",
    "plan": """\
category: cli_workflow
summary: Resolve and validate a job without writing project source files.
usage: patchshuttle plan JOB.psh.yaml [--diff]
preview: --diff prints a bounded unified diff of the final simulated bytes.
result: A successful plan does not mean that the job was applied.""",
    "html_lint": """\
category: local_policy
summary: Optionally lint the exact changed .html scope with djLint.
installation: Install patchshuttle[html].
configuration: Enable [linting.html] in patches/patchshuttle.toml.
safety: Jobs cannot enable, disable, or reconfigure linting; project djLint configuration is isolated.
behavior: Linting never reformats HTML and a failed transactional lint follows normal rollback policy.""",
    "formatting": """\
category: local_policy
summary: Format changed Python files with isort then Black after successful checks.
preflight: Plan records baseline and final-planned compatibility for each formatter and path.
exclusions: Exact .py paths may be set only in local isort_exclude or black_exclude lists.
safety: Jobs cannot disable formatters, add exclusions, or change formatter order and scope.
diagnostics: Baseline and patch incompatibility use distinct failure codes and retain bounded tool output.""",
    "ruff": """\
category: check
summary: Run the fixed Pyflakes F rule family over Python files changed by the current patch.
fields: none; the job shape is ruff: {}
planning: Scope is derived from non-NO_CHANGE patch actions, deduplicated in action order, and limited to .py file targets.
execution: Runs the current interpreter with -m ruff check --select F --no-fix -- and the immutable planned paths.
safety: Jobs cannot select other rules, enable fixes, supply paths, or expand the check to the repository.""",
    "django_import_check": """\
category: check
summary: Import validated dotted modules through the project's Django environment.
fields: manage_py (required path) and modules (required dotted identifiers)
execution: Runs the owner-selected project interpreter, or PatchShuttle's interpreter when no override is configured, with manage.py shell -c and controlled import code.
safety: No arbitrary Python expression or shell string is accepted from the job.""",
    "failure_logs": """\
category: cli_workflow
summary: Record validation and planning failures after a workspace is resolved.
locations: Timestamped VALIDATION_FAILED or PLAN_FAILED logs are written under patches/logs/.
contents: Failure code, stage, bounded diagnostics, summary, and AI handoff metadata.
limits: Invalid job source is not archived and workspace-discovery failures cannot create a workspace log.
usage: patchshuttle logs --last""",
    "workspace": """\
category: cli_workflow
summary: Route a command to one explicit initialized workspace.
usage: patchshuttle --workspace PATH COMMAND [ARGS]
resolution: Explicit paths are loaded as exact roots; otherwise the current directory and its parents are searched.
hinting: A bounded direct-child scan may suggest candidates but never selects one automatically.""",
}

EXPLAIN_TOPICS = tuple(_EXPLANATIONS)


def format_capability_list(values: tuple[str, ...]) -> str:
    """Render one stable machine-readable-looking capability list."""

    return "[" + ", ".join(values) + "]"


def render_capabilities() -> str:
    """Render the installed protocol surface without requiring a workspace."""

    return (
        "\n".join(
            (
                "PATCHSHUTTLE_CAPABILITIES",
                f"patchshuttle_version: {__version__}",
                "protocol: 1",
                f"job_kinds: {format_capability_list(JOB_KINDS)}",
                f"audit_actions: {format_capability_list(AUDIT_ACTIONS)}",
                f"change_actions: {format_capability_list(CHANGE_ACTIONS)}",
                f"checks: {format_capability_list(CHECKS)}",
                f"formatters: {format_capability_list(FORMATTERS)}",
                "html_lint: optional djlint extra; disabled by default local policy",
                "arbitrary_shell_action: unavailable",
                "protected_path_policy: local and not job-configurable",
                "schema_command: patchshuttle schema",
                f"explain_topics: {format_capability_list(EXPLAIN_TOPICS)}",
            )
        )
        + "\n"
    )


def render_schema() -> str:
    """Render the exact installed protocol model as deterministic JSON."""

    return (
        json.dumps(
            Job.model_json_schema(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def render_explanation(topic: str) -> str:
    """Render one bounded operation explanation by its canonical topic."""

    normalized = topic.casefold()
    try:
        body = _EXPLANATIONS[normalized]
    except KeyError as exc:
        raise ValueError(f"unknown explanation topic {topic!r}") from exc
    return f"PATCHSHUTTLE_EXPLAIN\ntopic: {normalized}\n{body}\n"


__all__ = [
    "AUDIT_ACTIONS",
    "CHANGE_ACTIONS",
    "CHECKS",
    "EXPLAIN_TOPICS",
    "FORMATTERS",
    "JOB_KINDS",
    "format_capability_list",
    "render_capabilities",
    "render_explanation",
    "render_schema",
]
