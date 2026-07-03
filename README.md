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
delete-cap and resume paths are live-tested against TCP OBS. The source is
opened read-only: with the default configuration the tool performs zero
mutating calls against the source store (see the safety section below). The Azure
source adapter is live-verified against a real Azure account (byte-identical
copies, MD5-checked). See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the
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

## Deploying it

To run the mirror yourself on a host between the two clouds (recommended: a TCP
ECS next to the destination bucket), follow [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md),
the full runbook covering the host, network, install, config, scheduling, and a
pre-flight checklist. Credentials are in [docs/CREDENTIALS.md](docs/CREDENTIALS.md).

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
   with per-object Content-Type carried across) then the delete phase.
   Resume is structural: a re-run re-diffs, and everything already copied is
   excluded automatically — the diff is the checkpoint.

## Source safety

- The engine wraps the source adapter in a read-only proxy; write/delete calls
  against the source cannot even be expressed. A dedicated tripwire test runs a
  full mirror and asserts zero mutating calls reach the source.
- The single opt-in exception is `rehydrate = true` (Azure Archive thawing,
  which permanently re-tiers source blobs). Default is `false`: archive blobs
  are skipped and reported, the source stays untouched, and a read+list SAS is
  all the tool needs.
- Deletes on the destination are capped (`max_delete`) and the source/dest
  prefixes must match when deletes are on, so a scoping mistake cannot mass-delete.

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

Full credential setup (OBS AK/SK, Azure account key vs SAS, least-privilege
scoping, systemd/cron deployment) is in [docs/CREDENTIALS.md](docs/CREDENTIALS.md).

## Configuration

All settings live in `config.toml`. Secrets are referenced by environment
variable name, not by value. See [config.example.toml](config.example.toml).

## License

MIT. See [LICENSE](LICENSE).
