# Security policy

## Supported versions

PatchShuttle is currently an alpha project. Security fixes are applied to the
latest published pre-release only. Older development archives are unsupported.

## Reporting a vulnerability

After the GitHub repository is published, use its **Security > Report a
vulnerability** form so details remain private. Do not include secrets,
credentials, private source code, or an exploit in a public issue. If private
vulnerability reporting is not enabled yet, contact the repository owner
privately and ask for a secure reporting channel before sharing details.

Include the PatchShuttle version, operating system, Python version, affected
command or API, minimal reproduction steps, observed result, expected result,
and whether project files or secrets may have been exposed.

## Security boundary

PatchShuttle is not an operating-system sandbox. Patch and verify checks run
local project code with the current user's permissions. A malicious or faulty
test, import, Django command, or user-defined profile can affect files and
services outside PatchShuttle's declared transaction.

PatchShuttle protects declared project paths with local policy, backups,
transaction checks, and rollback. It does not guarantee restoration of
external side effects. Log redaction recognizes common secret shapes but is
best-effort only. Review every AI-generated job before approval and inspect
every log before sharing it.
