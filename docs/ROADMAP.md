# Development roadmap after 0.1.0a6

This is a backlog, not a statement that every item ships in a7.
Published a6 evidence remains in RELEASE.md. Source version stays a6 until
an a7 candidate is qualified; no a7 package has been published.

## First a7 milestone

Implemented locally: conditional symbol-edit advisory in plans/full/compact logs,
operation selection rules, handoff example, MCP-aware retrieval guidance,
transfer/import telemetry, and smaller presentation modules.
The first guidance milestone passed hosted Linux/Windows/PostgreSQL CI.
Guide refresh now supports reviewed updates of recognized current/a4/a5/a6
templates with appended notes, hashes and backups; inline customizations require
manual review.
Pending: hosted qualification of guide refresh, clearer
indentation diagnostics, and final candidate/release qualification.

## Subsequent work, in priority order

1. Synchronize AI guide, protocol and explain output; detect stale generated
   guides and support reviewed refresh preserving owner additions.
2. Batch import edits with one initial guard; audit-derived action templates;
   explain conflicts with initial versus simulated intermediate state.
3. Static dependency/reference analysis before moves, unresolved dynamic cases,
   import-cycle reporting, reviewed updates and optional compatibility reexports.
4. End-to-end refactors: rename symbols/modules, extract selected declarations,
   move groups into packages, report architecture before/after.
5. Extend transfers: same-file moves, multiple destinations/anchors, move chains,
   case-only renames, binary files, multi-alias and selected conditional imports.
6. Improve indentation diagnostics; evaluate explicit dedented input and CST
   for targeted edits while preserving multiline-string semantics.
7. Deploy and test read-only MCP with real owner HTTPS/OAuth configuration;
   retrieve bounded diagnostics and the separate migration journal.
8. Migration data checks, partial-success reports, recovery integration and
   imported-helper identity. Do not claim file/database atomic rollback.
9. Reproduce the owner's original Windows isort error and add a regression test;
   cover Cyrillic paths, CRLF/BOM and subprocess output encodings.
10. Benchmark discovery/planning/checks/output size/retries before optimizing
    AST reuse, traversal or owner-configured focused check profiles.
11. Maintain reproducible dogfooding tasks with model/instruction versions,
    input state, timings, attempt counts, outputs and recovery outcomes.

## Release gates

Formatting, meaningful regression tests, full statement/branch coverage,
Ubuntu/Windows/PostgreSQL CI, build and clean-wheel checks, TestPyPI verification,
then a tagged GitHub prerelease and PyPI verification. Record evidence only after
completion. Do not weaken guards or architecture policy to pass these gates.
