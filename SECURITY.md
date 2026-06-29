# Security

## Credential handling

This tool talks to two cloud accounts. The rule is simple. Secrets stay out of
the command line, out of the repository, and out of logs.

How secrets are passed, in order of preference:

1. Environment variables. The process reads them at start. They do not show up in
   the process argument list, so `ps` and the shell history stay clean.
2. A local file that is gitignored (`.env` or `config.local.*`). Load it into the
   shell with `set -a; . ./.env; set +a`, or point the tool at it.
3. Short-lived tokens where the platform supports them (Azure SAS or user
   delegation, S3 STS). Prefer these over long-lived account keys.

Never do these:

- Pass an access key or secret as a command-line flag.
- Write a real key into any file that git tracks.
- Echo a secret into a log line or an error message.

## What must never be committed

The `.gitignore` blocks the common cases, but the human is the last check:

- access keys, secret keys, account keys, connection strings, SAS tokens
- `.pem`, `.key`, `.p12`, `.pfx`, `rclone.conf`
- `config.toml` (your real config) and any `.env`
- sync state files (`*.checkpoint`, `*.tsv`), they can embed object keys that map
  to real data paths

## Pre-commit guard

A hook in `.githooks/pre-commit` scans staged changes for key-shaped strings and
blocks the commit if it finds one. Turn it on after cloning:

```bash
git config core.hooksPath .githooks
```

Run the same scan by hand at any time:

```bash
scripts/check-secrets.sh
```

The guard reduces risk. It is not proof. Read your own diff before you push.

## Reporting

Found a leaked secret or a vulnerability in this code? Open a private report to
the maintainers rather than a public issue. If a credential reached a public
commit, rotate it first, then clean the history.
