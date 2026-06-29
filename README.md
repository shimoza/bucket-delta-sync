# bucket-delta-sync

One-way incremental sync between cloud object stores. It copies new and changed
objects from a source bucket to a destination bucket and removes from the
destination what no longer exists in the source, so the destination becomes a
true mirror of the source.

First target path: Azure Blob Storage to an S3-compatible object store, including
T Cloud Public (TCP) OBS. The design keeps the source and destination behind a
small backend interface, so other pairs can be added later.

## Status

Working. The engine is a custom Python tool over the cloud SDKs (boto3 for the
S3/OBS side, azure-storage-blob for the Azure side). The diff, copy, delete,
delete-cap and resume paths are live-tested against TCP OBS. The Azure source
adapter is implemented and unit-covered but still needs a live run against a
real Azure account. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the
engine decision and the design notes.

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
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

cp config.example.toml config.toml      # config.toml is gitignored
cp .env.example .env                     # .env is gitignored, holds secrets
# edit .env with your real keys, then load it into the shell:
set -a; . ./.env; set +a

# preview only, no writes (dry-run is the default):
python -m bucketsync --config config.toml

# perform the sync (copies and deletes, guarded by the delete cap):
python -m bucketsync --config config.toml --apply
```

The config selects the source and destination by `type`. Supported today:
`azure_blob` and `s3` (any S3-compatible store, including OBS) as source or
destination where it makes sense, plus `local_dir` for tests and demos. The
first target pair is `azure_blob` -> `s3`.

## Important: SDK checksum compatibility

Recent AWS SDKs (Go v2 `service/s3` >= v1.73.0, boto3 >= 1.36) send a default
CRC32 checksum in an `aws-chunked` body that OBS and some other S3-compatible
stores persist into the object, silently corrupting every upload. This tool
disables that behaviour, but **any other code your team points at OBS (a Go
backend, scripts) will hit it too.** See
[docs/CHECKSUM-COMPATIBILITY.md](docs/CHECKSUM-COMPATIBILITY.md) for the symptom
and the one-line fix.

## How it works

1. Stream both listings (sorted by key) and diff them by key and size in O(1)
   memory: source-only keys are copied, destination-only keys are deleted,
   size changes are recopied.
2. Check the delete cap. If the run would delete more than `max_delete`, it
   aborts before touching anything.
3. Dry-run prints the plan and stops. `--apply` runs the copy phase (threaded,
   resumable, Archive-tier blobs rehydrated first) then the delete phase.

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
