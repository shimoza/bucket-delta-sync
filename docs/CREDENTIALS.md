# Credentials

The guiding rule: **the config file never contains a secret.** It contains the
*name* of an environment variable, and the real value is read from the
environment at run time. So deploying credentials means setting environment
variables on the host and pointing the config at their names. Nothing secret
touches `config.toml`, git, the command line, or the logs.

```toml
# config.toml — names, never values
[source]
type      = "azure_blob"
account   = "<storage-account>"
container = "<container>"
key_env   = "SRC_AZURE_ACCOUNT_KEY"     # the NAME of an env var

[dest]
type           = "s3"
endpoint       = "https://obs.eu-de.otc.t-systems.com"
region         = "eu-de"
bucket         = "<dest-bucket>"
access_key_env = "DST_S3_ACCESS_KEY"    # name
secret_key_env = "DST_S3_SECRET_KEY"    # name
```

The tool calls `os.environ[<name>]` and fails loudly if the variable is unset,
so a misconfigured run stops instead of leaking or silently doing the wrong thing.

## Destination (OBS / S3): access key + secret key

What it needs: a permanent **Access Key (AK) + Secret Key (SK)** for a dedicated
service identity, scoped to the destination bucket only.

1. Create a dedicated service user for the sync. Do not reuse a person's keys.
2. Generate its AK/SK (on OTC: **My Credentials → Access Keys → Create Access
   Key**, or create the key under the service user in IAM).
3. Scope it with a **bucket policy** on the destination bucket: principal = that
   user, actions = **read + write + delete**. The mirror deletes orphaned
   objects, so delete is required. Grant nothing tenant-wide.
4. Set the two environment variables on the host:

   ```bash
   export DST_S3_ACCESS_KEY="<AK>"
   export DST_S3_SECRET_KEY="<SK>"
   ```

No session token is needed for a permanent AK/SK.

## Source (Azure Blob): account key or SAS

The tool only ever **reads** the source, so the minimum permission is
**List + Read** on the container. The adapter accepts the credential in two
forms. Pick one.

### Option A: storage account key (simplest, account-wide)

```bash
export SRC_AZURE_ACCOUNT_KEY="<account-key>"
```
```toml
[source]
key_env = "SRC_AZURE_ACCOUNT_KEY"
```
Get it from: storage account → **Security + networking → Access keys → key1**.
Downside: the account key grants full access to the whole storage account, not
just read. Fine for a quick test, over-privileged for a service.

### Option B: SAS token, container-scoped (recommended)

A Shared Access Signature limited to the one container, **read + list** only,
time-bound and revocable.

```bash
# generate a read+list SAS for the container
az storage container generate-sas \
  --account-name <storage-account> --name <container> \
  --permissions rl --expiry 2026-12-31T00:00Z \
  --auth-mode key --account-key "<account-key>" -o tsv
```
(or Portal → the container → **Shared access tokens** → permissions Read + List
→ Generate SAS token.)

The result is a query string like `sv=...&ss=...&sp=rl&sig=...`. Then:

```bash
export SRC_AZURE_SAS="sv=...&sp=rl&sig=..."
```
```toml
[source]
sas_env = "SRC_AZURE_SAS"      # use this INSTEAD of key_env
```

Use Option B for production: least privilege, scoped to one container, revocable.

### Archive tier note

If the source has **Archive-tier** blobs and you want the tool to rehydrate them
before copying, the Azure credential also needs **write / set-tier** permission
(add `w` to the SAS permissions, `rlw`). If the source is all Hot, `rl` is enough.

## Deploying the variables on a host

Do not `export` by hand for a long-running or scheduled service. Put the secrets
in a root-owned, `chmod 600` file and load it.

### systemd (recommended for a scheduled mirror)

```ini
# /etc/systemd/system/bucket-delta-sync.service
[Service]
EnvironmentFile=/etc/bucket-delta-sync/secrets.env   # chmod 600, root:root
ExecStart=/opt/bds/.venv/bin/python -m bucketsync --config /opt/bds/config.toml --apply
```

```
# /etc/bucket-delta-sync/secrets.env   (chmod 600)
SRC_AZURE_SAS=sv=...&sp=rl&sig=...
DST_S3_ACCESS_KEY=...
DST_S3_SECRET_KEY=...
```

### Manual or cron run

Use a gitignored `.env` (copy `.env.example`) and load it before the run:

```bash
set -a; . /opt/bds/.env; set +a
python -m bucketsync --config /opt/bds/config.toml          # dry-run
python -m bucketsync --config /opt/bds/config.toml --apply  # write
```

### Secrets manager

Resolve the values from a vault into the environment at launch (for example a
`pass` lookup or a cloud secrets manager), so they never sit on disk in clear.

## Verify

A dry-run exercises the credentials without writing anything. If a variable is
missing or wrong, it fails immediately with a clear message naming the variable.

```bash
python -m bucketsync --config config.toml      # dry-run, lists the planned delta
```

## Rotation

- OBS: rotate the AK/SK in IAM and update `DST_S3_*`. Keys can be rotated without
  code changes.
- Azure SAS: issue a new SAS before the old one expires and update `SRC_AZURE_SAS`.
  A leaked SAS is revoked by rotating the account key it was signed with, or by a
  stored access policy if you bind the SAS to one.
- Never commit any of these values. The repo ships a pre-commit secret scan
  (`scripts/check-secrets.sh`); keep it enabled with `git config core.hooksPath .githooks`.
