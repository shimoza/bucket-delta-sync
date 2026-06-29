# bucket-delta-sync

One-way incremental sync between cloud object stores. It copies new and changed
objects from a source bucket to a destination bucket and removes from the
destination what no longer exists in the source, so the destination becomes a
true mirror of the source.

First target path: Azure Blob Storage to an S3-compatible object store, including
T Cloud Public (TCP) OBS. The design keeps the source and destination behind a
small backend interface, so other pairs can be added later.

## Status

Early scaffold. The transfer engine is not selected yet. The repository ships
the credential-safe foundation, the configuration model, and the design notes.
See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the open engine decision and
the trade-offs behind it.

## What it does

- Incremental delta: only objects that are new or changed since the last run.
- True diff with deletes: the destination ends up matching the source, additions
  and deletions both.
- Cross-cloud aware: there is no server-side copy between different providers, so
  bytes are streamed through the host running the job.
- Safe by default: a dry-run mode and a cap on deletions, so a bad run cannot
  wipe the destination.

## What it does not do

- It is not a backup tool. Deletes propagate. Use destination versioning if you
  want a recovery window.
- It does not move credentials on the command line. See the security model below.

## Quick start

```bash
cp config.example.toml config.toml      # config.toml is gitignored
cp .env.example .env                     # .env is gitignored, holds secrets
# edit .env with your real keys, then load it into the shell:
set -a; . ./.env; set +a

# preview only, no writes:
# (engine command lands here once the engine is selected)
```

## Security model

Credentials never appear on the command line, in the repository, or in logs.

- Secrets come from environment variables, or from a local file that is
  gitignored and never committed.
- The example files carry placeholders only.
- A pre-commit hook scans staged changes for anything that looks like a key. See
  [SECURITY.md](SECURITY.md) and `scripts/check-secrets.sh`.

Enable the hook once after cloning:

```bash
git config core.hooksPath .githooks
```

## Configuration

All settings live in `config.toml`. Secrets are referenced by environment
variable name, not by value. See [config.example.toml](config.example.toml).

## License

MIT. See [LICENSE](LICENSE).
