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
    "find_files",
    "file_info",
    "hash",
    "git_status",
    "environment",
)
CHANGE_ACTIONS = (
    "create_directory",
    "create_file",
    "replace_exact",
    "insert_before",
    "insert_after",
    "delete_exact",
    "apply_diff",
)
CHECKS = (
    "compileall",
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
    "django_import_check": """\
category: check
summary: Import validated dotted modules through the project's Django environment.
fields: manage_py (required path) and modules (required dotted identifiers)
execution: Runs the current interpreter with manage.py shell -c and controlled import code.
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
