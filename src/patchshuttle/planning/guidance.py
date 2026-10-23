"""Bounded advisory guidance; never changes planning or execution decisions."""

PYTHON_SYMBOL_GUIDANCE = "For existing Python classes/functions/methods, prefer read_symbol + replace_symbol over text actions when the intended edit is one symbol replacement. Use python_structure only when the target is unknown. Preserve destination indentation; copy guards from a fresh audit."
RESULT_RETRIEVAL_GUIDANCE = "Retrieve the observed result through configured MCP history when available; otherwise request the resulting log. Never assume an MCP connection exists or infer success from a plan."
SYMBOL_ADVISORY = (
    "PYTHON_SYMBOL_ACTION_AVAILABLE: If editing an existing Python declaration, "
    "consider read_symbol + replace_symbol. Text/range actions remain valid "
    "for module assignments, comments and other non-declaration edits. "
    "After a targeting mismatch, obtain a fresh audit; do not weaken guards."
)


def symbol_advisories(evaluation) -> tuple[str, ...]:
    """Report targeting opportunities without claiming a symbol was detected."""
    if (
        evaluation.declared_python_text_actions
        and not evaluation.declared_python_symbol_actions
    ) or evaluation.failure_signal == "TEXT_TARGETING_FAILURE":
        return (SYMBOL_ADVISORY,)
    return ()


SYMBOL_WORKFLOW_EXAMPLE = """Python action fragments; audit first, then a separate reviewed patch:
- read_symbol:
    path: app/models.py
    symbol: Service.run
    max_bytes: 50000
    include_template: true
# Later patch: copy the actual audit hash; preserve signature and indentation.
- replace_symbol:
    path: app/models.py
    symbol: Service.run
    expected_ast_sha256: "<ast_sha256 from read_symbol>"
    new_content: |
          def run(self):
              return self.execute()
Example body only: adapt to audited project code. Do not submit placeholders.
Use the same Python minor version for audit and planning."""
