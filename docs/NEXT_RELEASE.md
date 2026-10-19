# Next release: structural editing and controlled refactoring

Status: implementation in progress, not a release qualification record.
Base: 0.1.0a5. Do not tag or publish this branch as a completed release.

## Implemented in this branch

`read_symbol` now reports `ast_sha256` and `ast_python`. `replace_symbol`
accepts `expected_ast_sha256` as an alternative to `expected_sha256`.
The AST digest ignores source positions, whitespace and ordinary comments;
it includes declaration names, decorators, annotations, defaults, docstrings
and function/class bodies. It is scoped to the Python major/minor version.
Obtain the hash from the same interpreter that will plan the edit.

At least one guard is required. If both guards are supplied, both must pass.
The existing exact-desired-content no-op rule remains in place. Old jobs
without an AST guard retain their serialized representation and identity hash.

AST-guarded replacements must contain exactly one declaration with the same
name and kind. The resulting module is compiled without execution and the
qualified symbol must still resolve uniquely. Source outside the selected
physical range is retained; existing BOM and newline policies still apply.
New content must use the correct indentation for its destination scope.
This is AST-selected source replacement, not AST unparse of the whole file.

```yaml
- replace_symbol:
    path: services/offers.py
    symbol: OfferService.calculate
    expected_ast_sha256: "<64 hex digits from read_symbol>"
    new_content: |
          def calculate(self, amount):
              return amount * 2
```

Use the text guard too when changes to comments must invalidate a patch.
No fuzzy matching or automatic selection of a similarly named declaration
is introduced. Imports and cross-file refactoring are not implemented here.

Formatter child interpreters and Black's stdin preflight explicitly use
`-I -X utf8`. This addresses locale-dependent Python stream encodings without
relying on environment variables ignored by isolated mode. This does not
establish the cause of any particular user's isort failure; retain and inspect
the original traceback before claiming that incident is resolved.

## Remaining implementation work

1. Structural imports: ensure/remove an exact import with alias awareness;
   preserve module docstrings and future-import ordering; reject ambiguous
   changes and validate the resulting module.
2. `move_file`: explicit source/destination, original byte hash, destination
   absence, retained mode, manifest-level deletion state, and rollback of
   both ends. Add deleted-path support to inventory comparison, architecture
   evaluation, formatter/check scope and manual rollback before exposing it.
3. `move_symbol`: select a class/function/method by qualified name and guard;
   move existing source including decorators; explicit destination scope;
   validate both modules and empty class bodies. Reject unsafe indentation
   transformations involving multiline string literals. Imports, closures,
   `super()`, decorators and relative imports need explicit dependency handling.
4. `move_range`: exact guarded physical range and explicit insertion anchor;
   preserve content and encoding; handle overlap and same-file offsets;
   include both files in one transaction and report source/destination ranges.
5. Refactoring preflight: report references and unresolved/dynamic dependencies.
   Update only references included in the reviewed plan. Run import checks,
   focused tests and architecture evaluation. Never claim arbitrary Python
   behavior is preserved merely because both files compile.

## Result retrieval: deployment choice still needed

The existing structured history is the source for retrieval. A read-only MCP
adapter or authenticated HTTPS endpoint can expose bounded `history.list`,
`history.latest` and `history.show` results. Do not include arbitrary file
reads, arbitrary command execution, or raw log uploads by default. Choose
where the adapter runs and how the AI client reaches it before implementing
a transport. Local stdio MCP and remote ChatGPT connections have different
deployment requirements. Existing CLI history is not itself an active remote
integration.

## Database migrations: adapter choice still needed

Choose initial support: Django migrations, Alembic, or explicit PostgreSQL
migrations. Keep DB execution separate from filesystem transactions.
Requirements for each supported adapter:

- Explicit database identity/alias and migration target, with credentials
  resolved by owner configuration rather than job content.
- A read-only plan and preconditions before execution; a lock and a durable
  journal with migration identity/checksum, start/end, outcome and checks.
- Distinguish not started, applied, failed and uncertain-after-interruption;
  do not automatically replay uncertain migrations.
- Explicit postconditions and repeat-run/idempotency behavior.
- Declare reversibility and backup/recovery requirements. Filesystem rollback
  must never be reported as a database rollback. Nontransactional operations
  need their own recovery procedure. Destructive reversals require an explicit
  requested operation; do not infer them from a failed test.

## Release gates

Retain docs/RELEASE.md gates: formatting, full tests with 100% branch coverage,
package and clean-wheel checks, Linux/Windows CI, then TestPyPI publication
and clean-index verification, then GitHub pre-release and production PyPI.
The current partial branch is not eligible for that publication sequence.
