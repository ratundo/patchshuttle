# PatchShuttle AI Guide

This file is the provider-neutral contract for ChatGPT and other AI services
that prepare PatchShuttle jobs for this project.

## Project identity

- Protocol: `1`
- Project ID: `{{PROJECT_ID}}`
- Canonical job extension: `.psh.yaml`

Every job must use exactly this project ID. Do not copy an ID from another
project or invent a replacement.

## Required AI response

When asked for a PatchShuttle job:

1. Return exactly one UTF-8 YAML document.
2. Do not wrap it in Markdown fences.
3. Do not add prose before or after the YAML.
4. Keep the job focused on one reviewable goal.
5. Use only the actions and checks listed below.
6. Never provide shell commands, Python code to execute the job, or policy
   overrides.
7. Preserve the same job ID when correcting a rejected job only when the user
   explicitly requests a correction of that same attempt.
8. Never add confirmation or rollback choices to YAML. `--yes` and
   `--keep-changes` are user-controlled CLI decisions, not job fields.
9. Never add formatter settings, formatter exclusions, or HTML linter settings
   to YAML. Those settings belong only to the user's local
   `patches/patchshuttle.toml` policy.

## Choose the operation before writing a job

For existing Python classes/functions/methods, prefer read_symbol + replace_symbol over text actions when the intended edit is one symbol replacement. Use python_structure only when the target is unknown. Preserve destination indentation; copy guards from a fresh audit.

| Intent | Preferred operation |
| --- | --- |
| Discover Python declarations | `python_structure` |
| Inspect a known declaration and obtain guards | `read_symbol` |
| Replace one existing declaration | `replace_symbol` |
| Move a declaration | `move_symbol` |
| Inspect/edit supported imports | `read_imports`, then `ensure_import` / `remove_import` |
| Move a file | `move_file` |
| Module assignment, comments, non-Python text, templates | Guarded text/range actions |
| New file | `create_file` |

`PYTHON_SYMBOL_ACTION_AVAILABLE` is advisory, not evidence that a particular
text edit targets a declaration. It never changes guards or rejects a job.

Audit action fragment (use an audit job):

```yaml
- read_symbol:
    path: app/models.py
    symbol: Service.run
    max_bytes: 50000
```

Later patch action fragment (use a separate patch job with project checks):

```yaml
- replace_symbol:
    path: app/models.py
    symbol: Service.run
    expected_ast_sha256: "<ast_sha256 copied from the audit>"
    new_content: |
          def run(self):
              return self.execute()
```

The replacement body is illustrative, not a claim about the project. Preserve
its actual signature and intended behavior. Use the same Python minor version
for audit and planning. Add `expected_sha256` from the audit when comment/source
changes must also invalidate the guard; both supplied guards must match.

Retrieve the observed result through configured MCP history when available; otherwise request the resulting log. Never assume an MCP connection exists or infer success from a plan.

## Job kinds

- `audit` - one or more read-only audit actions, no checks.
- `patch` - one or more change actions and appropriate checks.
- `verify` - one or more checks, no actions.

## Audit actions

- `tree` - bounded directory tree.
- `read` - bounded text-file excerpt.
- `search` - literal text search.
- `search_context` - literal search with bounded physical-line context.
- `read_symbol` - one exactly resolved Python symbol with range and hash.
- `python_structure` - bounded declaration-only Python structure discovery.
- `find_files` - bounded glob search.
- `file_info` - file metadata.
- `hash` - SHA-256 file hash.
- `hash_range` - canonical SHA-256 for a 1-based inclusive physical-line
  range.
- `git_status` - Git status when Git is available.
- `environment` - bounded development-environment summary.

### Python structure discovery workflow

Use `python_structure` when a task needs a declaration overview before source
reads. Prefer one feature directory or one `.py` file over the repository root,
and lower the explicit limits when a smaller map is sufficient:

```yaml
actions:
  - python_structure:
      path: src/example
      max_files: 50
      max_symbols: 400
      compact: true
```

`path` defaults to `.`, `max_files` to `300`, `max_symbols` to `2000`, and
`compact` to `false`. Directory and `.py` file targets are accepted. Prefer
`compact: true` for broad discovery: it keeps collection counts, limit signals,
file paths, import and symbol counts, symbol kinds, qualified names, and physical
ranges. Use full mode only when top-level import records, parameters, decorators,
or bases are needed. Parse errors are reported without source excerpts in both
modes.

After discovery, use `read_symbol` on the narrow qualified target instead of
broad source reads:

```yaml
actions:
  - read_symbol:
      path: src/example/service.py
      symbol: Service.run
```

Use the returned canonical SHA-256 only in a later guarded `replace_symbol`
patch. Do not infer calls, references, types, runtime behavior, or patch scope
from either declaration format. Project code is never imported or executed by
these audit actions. Compact mode changes only rendering; caching and persistence
are not implemented.
## Change actions

- `create_directory` - create one directory.
- `create_file` - create one new text file.
- `replace_exact` - replace an exact text occurrence count.
- `replace_symbol` - replace one guarded decorator-aware Python symbol.
- `insert_before` - insert text before an exact anchor.
- `insert_after` - insert text after an exact anchor.
- `delete_exact` - delete exact text with an expected count.
- `replace_range` - replace a guarded 1-based inclusive line range.
- `delete_range` - delete a guarded 1-based inclusive line range.
- `insert_at_line` - insert before or after one guarded physical line.
- `apply_diff` - apply a unified diff to existing text files.

Line-range operations are an optional complement to exact-content actions.
Use them only when a current audit established the exact physical line or
range. Line numbers position the operation but never prove identity. Every
change requires `expected_content`, `expected_sha256`, or both; when both are
present, both must match the planner's sequential simulated file state. There
is no fuzzy matching, relocation, partial application, or automatic line
shift. Ranges are 1-based and inclusive. Guard content is canonicalized to LF;
the digest is SHA-256 over canonical LF text encoded as UTF-8.

Prefer the read-only `hash_range` action when a digest guard is more compact
than returning a large source block. Copy its reported digest exactly:

```yaml
actions:
  - hash_range:
      path: src/example.py
      start_line: 120
      end_line: 135
```

A guarded replacement then uses the current range and digest:

```yaml
actions:
  - replace_range:
      path: src/example.py
      start_line: 120
      end_line: 135
      expected_sha256: 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
      new_content: |
        replacement
```

## Architecture ratchet

Local `[architecture]` configuration is owner authority and cannot be overridden
by a YAML job. During `plan`, PatchShuttle evaluates changed or created Python
modules and their direct parent packages against the virtual post-patch state.
Respect `WARNING` findings and correct `ERROR` findings rather than disabling the
policy or splitting files mechanically merely to satisfy a count.

Default stable codes are:

- `ARCH001` hard module growth and `ARCH002` module warning growth;
- `ARCH010` hard package growth and `ARCH011` package warning growth;
- `ARCH020` excessive new Python files and `ARCH021` excessive new packages.

Ratchet semantics permit an existing over-limit module or package to remain
unchanged or improve. Migrations and generated code are excluded by default.
Reports are bounded by `max_report_items`; `architecture_report_limited` tells
you whether more findings existed. Never infer dependencies, feature ownership,
semantic SRP, or a reorganization from these numeric findings alone.
## Structured execution history

Use structured history before loading a full execution log when investigating an
older change. Start with `patchshuttle history list --limit N`, then use
`patchshuttle history show JOB_ID/RECORD_ID` or `history latest [JOB_ID]`. Read the
referenced detailed log only when exact action output, traceback, timing, or other
diagnostic evidence is required.

Treat `declared.title`, `declared.intent`, planned files, and symbol targets as
submitted intent, not proof that the intended effect occurred. Treat `observed` as
PatchShuttle execution facts. Do not promote either field into product requirements,
user decisions, or semantic project memory. A generic external orchestrator may
index the JSON records together with its own separate sources.

Each attempt has a unique file under `patches/history/<job-id>/`. The record is
bounded, schema-versioned, and references the detailed log and derived AI-log view.
It excludes full stdout, full check output, patch contents, and large tracebacks.
History persistence is secondary and best-effort; `history_warning` in the Python
result means the job outcome remains authoritative but the external importer should
inspect the detailed log and retry ingestion according to its own policy.

Do not add inferred workarounds, relationships, affected symbols, semantic search,
or Farm-specific state to PatchShuttle history. Store those in the orchestrator.

## Checks

- `compileall`
- `ruff` - fixed `F` rules, no fixes, and only Python files changed by the current patch; use `ruff: {}`.
- `pytest` - optional arguments are limited to `-q`, `--quiet`, `-v`,
  `--verbose`, `-x`, `--exitfirst`, `-s`, `--disable-warnings`,
  `--strict-config`, `--strict-markers`, positive `--maxfail=N`,
  `--tb=auto|long|short|line|native|no`, and
  `--capture=fd|sys|no|tee-sys`.
- `unittest`
- `django_check` - full logs retain command output and add advisory
  `warning_analysis`, `known_warnings`, `new_warnings`, and
  `new_warning_details` fields. Manage explicit known W-class IDs with
  `patchshuttle warnings`, `--add ID`, and `--remove ID`. IDs are never
  accepted automatically and do not change check pass or fail status.
- `django_migrations_check`
- `django_test` - labels must be dotted Python identifiers.
- `django_import_check` - use for dotted modules that require Django settings
  or app-registry initialization; include `manage_py` and `modules`.
- `import_check`
- `profile` - only a profile already defined by the user in
  `patches/patchshuttle.toml`.

## Project Python interpreter

The workspace owner may set `execution.python_executable` in
`patches/patchshuttle.toml`. Relative values resolve from the workspace root;
absolute values remain absolute. When omitted, project checks use
PatchShuttle's current interpreter. Jobs cannot set or override this value.

The selected interpreter is used by `compileall`, `pytest`, `unittest`, the
Django checks, `import_check`, and `{python}` in local profiles. Ruff,
formatters, HTML lint, preflight, audits, and PatchShuttle internals keep using
PatchShuttle's own interpreter. PatchShuttle does not auto-detect a virtual
environment.
## Audit example

```yaml
protocol: 1
project_id: {{PROJECT_ID}}
id: AUDIT-001
kind: audit
title: Inspect the project structure
actions:
  - tree:
      path: .
      depth: 4
  - git_status: {}
```

## Patch example

```yaml
protocol: 1
project_id: {{PROJECT_ID}}
id: PATCH-001
kind: patch
title: Create a small Python module
actions:
  - create_directory:
      path: src/example
  - create_file:
      path: src/example/__init__.py
      content: |
        VALUE = 1
checks:
  - compileall:
      paths: [src]
```

## Verify example

```yaml
protocol: 1
project_id: {{PROJECT_ID}}
id: VERIFY-001
kind: verify
title: Run the project tests without changing files
checks:
  - pytest:
      paths: [tests]
      args: [-q]
```

When an import requires Django initialization, request the dedicated check:

```yaml
checks:
  - django_import_check:
      manage_py: manage.py
      modules: [email_client.views, email_client.urls]
```

## Local workflow

The user saves the YAML in `patches/inbox/`, validates it, runs
`patchshuttle plan`, and reviews the read-only plan locally. The user may add
`--diff` to review the bounded final resolved diff before execution. An audit
can run without confirmation. Patch and verify jobs require explicit
confirmation or deliberate `--yes` automation. If validation or planning
fails after workspace resolution, ask for the generated failure `.log`; its
path is printed in the terminal and available through
`patchshuttle logs --last`. Prefer `patchshuttle logs --last --ai` for a
compact deterministic text view or `--ai-json` for machine-readable output;
the original full log remains unchanged.

Treat `PYTHON_DISCOVERY_EVALUATION` as evidence from only the current job.
Compare its explicit Python paths, audit output volume, targeting counts,
reason codes, and failure signal across multiple real-project logs. Do not
interpret `index_assessment: NOT_EVALUATED` as a recommendation, and do not
claim measured token savings from these fields.

Compare reported Python file/search matches, result-limit fields, and audit
duration only between jobs with equivalent actions and bounds. Treat evidence
as contaminated when owner-controlled `project.ignored_paths` still allow
environments, generated copies, or backups into traversal. Do not ask a job
to hide paths dynamically: exclusions remain local workspace policy.

Use reported exact line numbers,
nearby similarity
matches, or
unified-diff hunk diagnostics to correct the next job instead of guessing file
content. `LINE_RANGE_OUT_OF_BOUNDS` and `LINE_RANGE_GUARD_MISMATCH` require a
fresh audit; never shift or weaken the guard by guessing. After every recorded
attempt, retrieve configured MCP history when available, or request the `.log`
file or a fresh `patchshuttle handoff` file. When reading a log, use its final
`PATCHSHUTTLE_AI_HANDOFF` block before preparing the next job. Never state that
a job was applied merely because YAML or a successful plan was created.

When operation syntax is unclear, ask the user to run
`patchshuttle capabilities`, `patchshuttle schema`, or
`patchshuttle explain TOPIC`. These commands inspect the installed contract
without requiring audit access to protected generated documentation. If the
current directory is outside the project, the user can place a global
`--workspace PATH` option before the command. A reported child-workspace hint
is advisory; never assume that candidate was selected.

Read the formatter matrix in every plan. `RUN` means that formatter will touch
that exact path. `SKIP_LOCAL_POLICY` is an owner-controlled exception and must
not be copied into YAML. `FORMATTER_PATCH_INCOMPATIBLE` means the planned
content is incompatible after a compatible or absent baseline, so correct the
patch. `FORMATTER_BASELINE_INCOMPATIBLE` means the file was already
incompatible and remains so; do not ask to bypass safety policy. The owner may
deliberately add an exact local exclusion, or the next patch may repair the
file. A plan with `baseline=INCOMPATIBLE, planned=PASS` is a valid repair.

Interpret failure states conservatively:

- `ROLLED_BACK` means declared transaction paths were restored, but reported
  external check side effects still require review.
- `SKIPPED_CHANGES_KEPT` means the user explicitly retained partial declared
  changes. Request a fresh audit or handoff before producing a corrective job.
- `SKIPPED_NO_CHANGES` means rollback was skipped before a declared change was
  published; still use the log as the authoritative result.
- `ROLLBACK_FAILED` means restoration is incomplete. Stop proposing new
  changes until the user resolves the listed paths.

This build provides workspace initialization, validation, read-only
planning, bounded audit execution, approved patch execution, and approved
one-pass verification. Patch execution uses all text change actions,
optional changed-HTML linting under local policy, controlled initial checks,
per-tool changed-Python isort then Black scopes, final checks, bounded
before/after workspace inventory, defensive runtime-cache cleanup, undeclared
side-effect reporting, rollback of declared transaction paths, exact CLI job
archives, an atomic registry, and fixed-section redacted logs. Planning records
PEP 263 Python encoding plus baseline and final-planned formatter compatibility
before confirmation. HTML content is linted through
stdin from an isolated configuration root, so a job cannot weaken local lint
policy through project djLint settings. Completed patches also support guarded
manual rollback. An explicitly user-approved `--keep-changes` run can
retain partial declared changes after failure and records that decision
distinctly. Snapshot and handoff commands produce bounded context without
dumping source contents. The same completed job ID and normalized hash returns
`ALREADY_APPLIED`; the same ID with different content returns
`PATCH_ID_CONFLICT`. An `UNEXPECTED_WORKSPACE_CHANGE` result means the reported
external path must be reviewed separately; for a normal patch, declared paths
are rolled back. Redaction is best-effort. It masks common credential shapes
and sensitive Python assignments, including prefixed secret identifiers and
one-line secret collections, while preserving safe references and loader calls.
Always review the resulting log; do not tell the user that it is guaranteed to
contain no secrets.

Early `VALIDATION_FAILED` and `PLAN_FAILED` logs do not archive invalid source,
update the registry, report project changes, or create backups. Successful
standalone validation and planning do not create logs, and workspace discovery
failures cannot write a log before a workspace is selected.

See `PATCHSHUTTLE_PROTOCOL.md` and `patchshuttle.schema.json` for the detailed
syntax.

## Structural symbol guards

`read_symbol` reports `ast_sha256` and `ast_python` in addition to the text
hash. Use `expected_ast_sha256` for formatting-independent `replace_symbol`
guards on the same Python minor version. Ordinary comments are not guarded;
include `expected_sha256` too if exact comment/source preservation matters.
Both supplied guards must match. Keep the symbol name, declaration kind and
destination indentation in `new_content`. The final module is compiled without
execution. The AST guard itself does not rewrite imports; use the explicit transfer actions below for source moves.


## Guarded source transfers

- `move_range`: `path`, `start_line`, `end_line`, source `expected_content`
  and/or `expected_sha256`, `destination`, `destination_line` and
  `expected_destination_sha256`.
- `move_symbol`: `path`, `symbol`, `expected_ast_sha256`, `destination`,
  `expected_destination_sha256` and optional `destination_class`.

Destination hashes cover simulated raw bytes; source range hashes cover
canonical LF/UTF-8 text. Destinations must exist or be created earlier in the
same job. Range insertion is before the 1-based destination line; EOF+1 appends.
Symbol insertion appends at module scope or to the named class. Both files are
validated and participate in the same backup/check/rollback transaction.

Same-file transfers, case-only path differences, duplicate destination names,
closure extraction, class-bound super/private-name moves and multiline-string
reindentation are rejected. Imports and callers are not automatically rewritten;
include explicit edits and focused project checks. These actions move source
content and leave the source file present. Whole-file moves are separate work.


## Whole-file transfers

`move_file` moves an existing supported text file to an absent destination.
Supply `path`, `destination`, and `expected_sha256` of the original raw bytes.
The transaction preserves original bytes and permissions, creates missing
parent directories, and reports the source in `deleted_files`. Formatters and
checks target the surviving destination. Add explicit import and caller edits
when moving Python modules; imports are not automatically rewritten.

The source and destination must not have been touched earlier in the job.
Subsequent edits to the destination are supported. Case-only moves, chains,
swaps, binary files and reuse of the vacated path are currently rejected.
A failed check restores the source and removes the destination. Manual rollback
also verifies the applied state and refuses to overwrite a recreated source.


## Structural import edits

`read_imports` accepts `path` and optional `max_bytes`. It reports top-level
imports, `imports_ast_sha256`, and `ast_python`. The hash ignores formatting
and comments, includes alias names and order, and is Python-minor-specific.
Nested/conditional imports are not selected. Use the same interpreter for
inspection and planning.

`ensure_import` and `remove_import` require `path`, `statement` and
`expected_imports_ast_sha256`. The statement must be exactly one Python
`import` or `from ... import ...` with one alias, optionally renamed. Relative
imports are supported. Star requests and ambiguous duplicate matches fail.
The guard is always checked, including for already-present/absent imports.

```yaml
- ensure_import:
    path: services/offers.py
    statement: from .pricing import calculate_price
    expected_imports_ast_sha256: "<64 hex digits from read_imports>"
```

An existing alias in a multi-alias import satisfies `ensure_import` without
rewriting that line. Removal from a multi-alias import requires a separately
reviewed rewrite. Removal retains comments, including comments inside a
parenthesized import. Shared semicolon lines are rejected when editing them.
Insertion preserves headers, the module docstring and future-import ordering.
Existing bindings are checked conservatively across scopes; a conflicting
local name may therefore require a separately reviewed edit too. The resulting
module is compiled without importing or executing it.

Unrelated source remains intact before the normal scoped formatter phase.
Neither action rewrites callers, proves runtime compatibility, nor infers
imports when moving files or symbols. A sequence of import edits needs the
hash of each simulated intermediate import set, or separate audited jobs.
