# Deployment

End-to-end guide to run the mirror yourself on a host between the two clouds.
The recommended target is an Elastic Cloud Server (ECS) on T Cloud Public (TCP),
next to the destination OBS bucket. All names below are placeholders, fill in
your own.

## How it runs (topology)

There is no server-side copy between different clouds, so the tool is a
**through-host mover**: it downloads each object from the source and uploads it
to the destination, and every byte transits the one host running the script.

```
Source (Azure Blob) ──GET──► ECS running bucket-delta-sync ──PUT──► Destination (OBS)
                              diff in RAM (streaming), plan on local disk
```

Run it on a TCP ECS in the same region as the destination bucket. The OBS side
(writes, deletes, destination listing) is then local and intra-region. Only the
source side crosses the internet.

## 1. The host (TCP ECS)

The mirror streams the diff and pipes bytes through, so it is I/O-bound, not CPU
or memory hungry.

| Resource | Recommended | Notes |
|---|---|---|
| Flavor | `s3.xlarge.2` (4 vCPU / 8 GB) | general purpose; `c7.xlarge.2` if you prefer compute-optimized |
| RAM | 8 GB | the diff streams, so 6.2M objects do not go into memory |
| System disk | 40 GB | holds the plan + checkpoint files, **not** the data |
| OS | Linux, Python 3.11+ | tested on 3.12 |

For a one-shot **full** copy through the tool (not just deltas), pick a 16 GB
flavor (`s3.xlarge.4`) for checkpoint headroom. For the recurring delta mirror,
8 GB is comfortable.

**It does NOT need** a big-RAM box or terabytes of disk. The tool never stages
data on local disk, it streams source-to-destination. Disk only holds the
copy/delete plan (a few hundred MB at full scale, tiny for a delta).

## 2. Network

- **Outbound HTTPS (443) to the source over the internet.** The ECS needs an
  **EIP or a NAT gateway** to reach the source storage endpoint. Size the
  bandwidth to the delta volume, not the full dataset.
- **OBS** is reached on the **internal regional endpoint**
  (`https://obs.<region>.otc.t-systems.com`), not via the EIP, and is not
  EIP-throttled.
- Security group: allow outbound 443 to both.

## 3. Install

```bash
sudo apt-get update && sudo apt-get install -y python3-venv git   # or dnf
git clone https://github.com/shimoza/bucket-delta-sync.git
cd bucket-delta-sync
git config core.hooksPath .githooks          # enable the secret-scan hook
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
```

## 4. Configure

```bash
cp config.example.toml config.toml           # config.toml is gitignored
```

Edit `config.toml` (names only, never secrets):

```toml
[source]
type      = "azure_blob"
account   = "<storage-account>"
container = "<container>"
sas_env   = "SRC_AZURE_SAS"       # or key_env, see docs/CREDENTIALS.md
prefix    = ""                     # optional, limit to one path

[dest]
type           = "s3"
endpoint       = "https://obs.<region>.otc.t-systems.com"
region         = "<region>"
bucket         = "<dest-bucket>"
provider       = "HuaweiOBS"
access_key_env = "DST_S3_ACCESS_KEY"
secret_key_env = "DST_S3_SECRET_KEY"

[sync]
compare    = "key_size"
delete     = true                  # mirror: remove from dest what is gone from source
max_delete = "0.5%"                # abort if a run would delete more than this
threads    = 16
dry_run    = true                  # default; --apply overrides
state_dir  = ".state"
```

## 5. Credentials

Full detail in [CREDENTIALS.md](CREDENTIALS.md). In short:

- **OBS**: a dedicated service AK/SK, scoped by bucket policy to the destination
  bucket with read + write + delete. Set `DST_S3_ACCESS_KEY` / `DST_S3_SECRET_KEY`.
- **Source (Azure)**: a container-scoped SAS with read + list (recommended), or a
  storage account key. Set `SRC_AZURE_SAS` (or `SRC_AZURE_ACCOUNT_KEY`).

Deliver them as environment variables, never on the command line. For a service,
use a `chmod 600` env file (see CREDENTIALS.md, systemd section).

## 6. Run

Dry-run first, always. It lists the planned delta and writes nothing.

```bash
set -a; . ./.env; set +a                      # load secrets into the env
python -m bucketsync --config config.toml      # DRY RUN
python -m bucketsync --config config.toml --apply   # perform the sync
```

Run inside `tmux` or `screen`, a full pass over millions of objects is long. If
interrupted, just re-run, it resumes from the checkpoint.

## 7. Schedule the recurring mirror

systemd service + timer (secrets in a `chmod 600` env file):

```ini
# /etc/systemd/system/bucket-delta-sync.service
[Service]
Type=oneshot
EnvironmentFile=/etc/bucket-delta-sync/secrets.env
WorkingDirectory=/opt/bds
ExecStart=/opt/bds/.venv/bin/python -m bucketsync --config /opt/bds/config.toml --apply
```

```ini
# /etc/systemd/system/bucket-delta-sync.timer
[Timer]
OnCalendar=*-*-* 02:00:00          # nightly at 02:00
Persistent=true
[Install]
WantedBy=timers.target
```

```bash
sudo systemctl enable --now bucket-delta-sync.timer
```

(Or a cron entry that sources the env file and runs the same command.)

## 8. Safety and operations

- **Dry-run is the default.** Writing requires `--apply`.
- **Delete cap.** `max_delete` aborts a run that would remove too much, so a
  source-side listing glitch can never wipe the destination. Keep it low.
- **OBS versioning** on the destination bucket is the recommended backstop. It
  does not stop a delete, it keeps the previous bytes recoverable.
- **Resume.** The copy phase checkpoints; a re-run skips what already copied.
- **Tune `threads`** to the EIP bandwidth.
- **Checksum compatibility.** The tool already disables the AWS-SDK default
  checksum that corrupts OBS uploads. If you point any *other* code at OBS, read
  [CHECKSUM-COMPATIBILITY.md](CHECKSUM-COMPATIBILITY.md).

## 9. What sizes the runtime

The binding cost of each pass is **listing both sides**, not the byte transfer.
At a few thousand keys per second a full pass spends minutes to tens of minutes
listing before it copies the delta. That sets how often you can usefully run it.
Near-real-time later means event-driven detection (source change feed) instead
of re-listing, which this tool can grow into.

## 10. Archive tier

If the source has Archive-tier objects and you want them rehydrated before copy,
the source credential needs set-tier permission (add `w` to the SAS). If the
source is all hot, ignore this.

## Pre-flight checklist

- [ ] ECS up in the destination region, EIP/NAT for outbound 443
- [ ] Python 3.11+, repo cloned, venv, `pip install -r requirements.txt`
- [ ] `git config core.hooksPath .githooks`
- [ ] Destination bucket exists, versioning on, bucket policy scoped to the service AK/SK
- [ ] Source SAS (read+list) issued and not expiring mid-migration
- [ ] Secrets in a `chmod 600` env file, referenced by name in `config.toml`
- [ ] `max_delete` set conservatively
- [ ] Dry-run reviewed before the first `--apply`
