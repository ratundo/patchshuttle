# Controlled Django migrations

Install `patchshuttle[django]` in the project's interpreter and add
`"patchshuttle.django_app"` to the owner's `INSTALLED_APPS`. The adapter has no
models and adds no migration of its own. Initialize a PatchShuttle workspace
and run the management command from the Django project as usual.

Preview an exact target (prefix matching is deliberately unsupported):

```bash
python manage.py patchshuttle_migrate orders 0007_add_status --workspace . --database default
```

The JSON plan contains ordered operations, direction, atomicity, declared
reversibility, migration-file checksums, current applied migrations and a database
fingerprint. Credentials come from Django settings and are not written into the
plan. Planning loads trusted Django project/migration code and reads database
state; the adapter does not apply schema operations during preview. Application
startup hooks remain project code and may have their own effects.

Review the plan and prepare a tested database backup/recovery procedure. Apply
using the exact returned SHA-256 and a non-secret reference to that procedure:

```bash
python manage.py patchshuttle_migrate orders 0007_add_status --workspace . --database default --apply --expected-plan <sha256> --recovery-reference <backup-or-recovery-record>
```

The reference records the owner's recovery preparation; PatchShuttle does not
create or verify the backup. A workspace lock and database lock are held while
rechecking the plan, running Django checks, applying migrations and verifying
both the exact applied set and unchanged migration sources. System checks run
before and after migration. Add domain-specific checks to Django's check
framework; model checks alone do not validate business data.

PostgreSQL uses a session-level advisory lock shared by cooperating adapter
processes in that database. Persistent SQLite uses a companion file lock.
Ordinary `manage.py migrate`, other applications and manual SQL do not observe
these locks. Use one canonical workspace/journal for each database's migration
operations. PostgreSQL connection poolers must preserve the same server session
for the operation; transaction-pooling mode is not supported.

Initial limits: PostgreSQL and persistent SQLite, autocommit connections,
transactional DDL and atomic migrations. In-memory/URI SQLite, other backends,
non-atomic migrations, unresolved graph conflicts and squashed replacements
are rejected. Migration checksums cover migration source files, not imported
helper implementations or arbitrary application dependencies. Keep the deployed
code immutable during planning/execution.

## Failure and recovery

Records are separate from filesystem patch backups, under
`patches/state/django-migrations`. An exclusive, flushed `*.start.json` precedes
migration execution; a separate `*.result.json` records success or failure.
An unfinished start record or `FAILED_REVIEW_REQUIRED` blocks another execution.
The adapter does not automatically retry an interrupted operation or reverse
already applied migrations. A multi-migration plan can partially succeed before
failure even if each individual migration is atomic. Python migration code may
also have external effects that database transactions cannot undo.

After independently inspecting/recovering the database, generate a fresh plan
and explicitly record owner reconciliation:

```bash
python manage.py patchshuttle_migrate orders 0007_add_status --workspace . --database default --reconcile <attempt-id> --expected-plan <fresh-sha256> --recovery-reference <verified-recovery-record>
```

This appends an acknowledgement. It does not repair, replay or prove the
correctness of database contents. Corrupt journals must be investigated;
do not delete them just to bypass an unresolved attempt.

## Explicit database reversal

Choose the earlier target (or `zero`) and preview with `--reverse`. Apply that
reviewed reverse plan with the same `--reverse`, hash and recovery reference.
An irreversible operation is rejected. Django's reversible flag does not imply
that data lost by forward operations can be restored. Reversal can remove data
and requires separate review and backup planning.

Filesystem `rollback` never reverses the database. The MCP history adapter
currently retrieves filesystem-job history; this separate migration journal
is not yet exposed through MCP.

Django's migration behavior: https://docs.djangoproject.com/en/6.0/topics/migrations/
