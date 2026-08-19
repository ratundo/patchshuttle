"""Internal HTML template linting surface."""

from patchshuttle.linters.runner import (
    HtmlLintResult,
    HtmlLintRunResult,
    HtmlLintStatus,
    PreparedHtmlLint,
    prepare_html_linter,
    run_html_linter,
)

__all__ = [
    "HtmlLintResult",
    "HtmlLintRunResult",
    "HtmlLintStatus",
    "PreparedHtmlLint",
    "prepare_html_linter",
    "run_html_linter",
]
