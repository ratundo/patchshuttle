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
is introduced. Structural import editing remains outstanding; file moves and cross-file source transfers are described below.

Formatter child interpreters and Black's stdin preflight explicitly use
`-I -X utf8`. This addresses locale-dependent Python stream encodings without
relying on environment variables ignored by isolated mode. This does not
establish the cause of any particular user's isort failure; retain and inspect
the original traceback before claiming that incident is resolved.

## Remaining implementation work

1. Structural imports: ensure/remove an exact import with alias awareness;
   preserve module docstrings and future-import ordering; reject ambiguous
   changes and validate the resulting module.
2. Extend `move_file` beyond the current guarded text-file scope only after
   adding corresponding binary-content and chained-move validation.
3. Extend implemented `move_symbol`: select a class/function/method by qualified name and guard;
   move existing source including decorators; explicit destination scope;
   validate both modules and empty class bodies. Reject unsafe indentation
   transformations involving multiline string literals. Imports, closures,
   `super()`, decorators and relative imports need explicit dependency handling.
4. Extend implemented `move_range`: exact guarded physical range and explicit insertion anchor;
   preserve content and encoding; handle overlap and same-file offsets;
   include both files in one transaction and report source/destination ranges.
5. Refactoring preflight: report references and unresolved/dynamic dependencies.
   Update only references included in the reviewed plan. Run import checks,
   focused tests and architecture evaluation. Never claim arbitrary Python
   behavior is preserved merely because both files compile.

## Result retrieval and Django migrations

The owner selected ChatGPT retrieval and Django migrations. Optional adapters
are now implemented. See `CHATGPT_INTEGRATION.md` for OAuth resource-server
configuration and `DJANGO_MIGRATIONS.md` for migration review, journal and recovery.
The user's live OAuth/ChatGPT deployment is not configured. PostgreSQL 16
qualification runs in the new CI service job; local integration tests use SQLite.

## Release gates

Retain docs/RELEASE.md gates: formatting, full tests with 100% branch coverage,
package and clean-wheel checks, Linux/Windows CI, then TestPyPI publication
and clean-index verification, then GitHub pre-release and production PyPI.
The current partial branch is not eligible for that publication sequence.

## Cross-file source transfers (implemented after the initial draft)

`move_range` transfers an exact guarded line range into another file before
`destination_line` (1-based; EOF+1 appends). `move_symbol` transfers a qualified
Python class/function/method, appending to a destination module or to
`destination_class`. A source class left without statements receives `pass`.
Both actions require `expected_destination_sha256`, the SHA-256 of the
simulated raw destination bytes. Use audit `hash`; a destination created earlier
in the job needs the hash of its declared encoded content. Source ranges use
`expected_content` and/or `expected_sha256`; symbols use `expected_ast_sha256`.

Destinations must exist or be created by a preceding `create_file`. Both files
participate in the existing backup/check/rollback transaction. Each keeps its
own encoding/newline convention. Range moves do not silently add missing
newlines. Symbol moves may add a separator/final newline and adjust indentation;
the moved declaration must retain its AST. Reindent of multiline string tokens
is rejected. Transfers within the same file and case-only filename differences
are not supported. These actions transfer source content, not whole files.

These operations do not infer imports, rewrite callers or prove runtime
behavior. Add explicit dependency edits and focused import/tests in the same
job. Missing/duplicate symbols, name collisions, nested closure extraction,
class-bound `super`, `__class__` and private-name moves are rejected.

```yaml
- create_file:
    path: services/pricing.py
    content: ""
- move_symbol:
    path: services/offers.py
    symbol: calculate_price
    expected_ast_sha256: "<AST hash from read_symbol>"
    destination: services/pricing.py
    expected_destination_sha256: e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
```

The placeholder source digest above must be replaced by the actual audit value.
The destination digest is the SHA-256 of an empty byte sequence.


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
