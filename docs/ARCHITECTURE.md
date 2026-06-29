# Architecture and design notes

## Goal

One-way incremental sync from a source bucket to a destination bucket, where the
destination ends up as a true mirror of the source. New and changed objects are
copied. Objects removed from the source are removed from the destination. The
first target pair is Azure Blob Storage to an S3-compatible store, including
T Cloud Public (TCP) OBS.

## The delta problem is a moving target

A bucket sync is a batch job. The source keeps changing while the job runs. So a
single pass can never reach zero difference against a live source. The gap is
structural, not a bug. The job is to bound the gap and to close it at one chosen
moment, the cutover.

The gap has four parts, and they close differently:

1. Arrivals during the pass. A pass takes time T to scan and copy. At write rate
   R, about R x T new objects land before it finishes. This dominates the first
   full pass.
2. Snapshot staleness. A listing is a point-in-time view that is already stale
   when it returns. An object mid-upload may be missing or show a partial size.
3. Detection lag. With listing-based diff, a change is not seen until the next
   pass starts, so worst-case detection latency is one pass interval.
4. In-flight write URLs. If clients write directly to storage with pre-signed
   URLs, a URL minted before cutover stays valid for its lifetime. A write can
   land in the source up to that TTL after the backend stops minting new ones.

Per pass, the backlog shrinks geometrically, about N x (R/C)^n where C is copy
rate. It converges only if C is greater than R, that is the host copies faster
than the source is written. Even then there is a floor set by the listing
overhead per pass plus the in-flight objects. The floor is what decides whether
near-real-time behaviour is reachable.

Two levers shrink the floor:

- Cheaper, more frequent passes. Re-listing millions of objects each pass keeps
  the overhead high. Event-driven detection (a change feed or event
  notifications) turns the delta into draining a queue, which cuts the overhead
  from minutes to seconds.
- Do not require zero difference before cutover. A read-fallback during the
  transition (read destination first, fall back to source on a miss) masks the
  gap for readers, so the live gap only needs to drain, not to vanish.

The gap reaches zero at one moment, the freeze window. Stop new source writes,
run the final delta, verify zero difference, then switch reads. The freeze length
must be at least the write-URL TTL plus the time to drain the residual backlog.
So the residual gap and the URL TTL size the freeze window. Those two numbers are
worth measuring before cutover.

## Diffing strategy

The default comparison is key plus size, not ETag and not modification time.

- ETag is unreliable across providers and after server-side changes. An S3
  multipart upload produces a composite ETag, not a plain MD5, and re-encryption
  changes it. Azure and S3 do not share a hash, so a cross-cloud checksum
  comparison forces a re-transfer of everything.
- Modification time is per-provider and subject to clock skew. It is fine as a
  per-side relative signal, not as a cross-cloud absolute.
- Key plus size catches new objects, deletes, and most content changes for this
  workload. For an append-mostly store with unique keys, an object rarely
  changes in place, so size is a strong signal.

A storage inventory report (S3 Inventory, Azure Blob Inventory) is the cheaper
way to diff at very large object counts, because it avoids live LIST calls. It is
a candidate for the scaled-up version. An event-driven feed is the path to
near-real-time, see the moving-target section.

## Deletes, done safely

Deletes propagate so the destination mirrors the source. That is powerful and
dangerous, so the guardrails are not optional:

- Dry-run is the default. A run prints what it would add and delete and writes
  nothing until told to.
- A delete cap aborts the run if it would remove more than a set number or
  percentage of objects. A source-side listing glitch should never be able to
  empty the destination.
- Destination versioning is the recommended backstop. It does not stop a delete,
  it keeps the previous bytes recoverable. Cost is paid only when an overwrite or
  delete actually happens.

## Cross-cloud realities

- No server-side copy between different providers. Bytes are downloaded from the
  source and uploaded to the destination through the host running the job. Plan
  for egress cost on the source side and for host bandwidth.
- Throttling is normal at scale. Back off on 429 and 503, cap concurrency, retry
  with jitter.
- Listing and diffing millions of objects must not hold everything in memory.
  Stream and sort on disk, or partition by prefix.

## Cold and archive tier

Archive-tier objects on the source cannot be read until they are rehydrated, and
rehydration takes hours. Any tool either skips them or fails on them. If the
source has archive-tier objects, the sync needs a rehydrate-then-copy step. If
the source is all hot, this whole concern disappears. Confirm the source tier mix
before choosing the engine.

## Engine options

Managed migration services do not fit this exact pair. They are listed so the
choice is on the record:

| Option | Azure source | OBS / S3-compatible destination | Deletes | Verdict for this job |
|---|---|---|---|---|
| azcopy | yes | no, Azure is the destination only | n/a | Cannot target OBS |
| AWS DataSync | yes, as a source | AWS storage only as destination | yes | Cannot target OBS |
| Azure Storage Mover | targets Azure | no | n/a | Cannot target OBS |
| S3 Batch / replication | no | within AWS S3 only | n/a | Not cross-cloud |
| rclone | native | native, provider HuaweiOBS | native sync | Strong fit |
| Custom (Python SDKs) | via SDK | via SDK | own logic | Full control, more to own |

So the realistic choice narrows to rclone or a custom tool.

- rclone covers the source and destination natively, does delete-sync with a cap
  and a backup directory, and reads credentials from a config file or env, not
  from the command line. It cannot read archive-tier source objects and does not
  transform objects in flight. At millions of objects it needs memory tuning, for
  example avoiding the all-in-memory listing mode.
- A custom tool gives full control over diffing, resume, archive rehydration, and
  any per-object step. The cost is owning the retry, throttling, and multipart
  logic that rclone already hardened.

## Open decision

The engine is not selected. The decision waits on one fact, the source tier mix,
and on confirming whether an event-driven feed is wanted for near-real-time
behaviour. Three shapes are on the table:

1. Thin wrapper over rclone. A small command-line tool that loads credentials
   safely, pins the destination-safe flags, runs a dry-run verify, and optionally
   pre-rehydrates archive objects. Least code, native deletes, clean credentials.
2. Custom tool on the cloud SDKs. A sorted-merge diff with copy and delete, own
   checkpoint and rehydration. Most control, most code to maintain.
3. rclone plus a runbook. No custom code, a documented config and command set.
   Smallest deliverable, discipline rests on the operator.

This document is updated when the decision lands.

## Sources

- rclone Azure Blob backend and archive limitation: https://rclone.org/azureblob/
- rclone sync, delete cap and backup dir: https://rclone.org/commands/rclone_sync/
- rclone S3 providers, including HuaweiOBS: https://rclone.org/s3/
- rclone listing and memory at scale: https://rclone.org/docs/
- azcopy, S3 as source to Azure only: https://learn.microsoft.com/en-us/azure/storage/common/storage-use-azcopy-s3
- AWS DataSync Azure Blob to Amazon S3: https://aws.amazon.com/blogs/storage/migrating-azure-blob-storage-to-amazon-s3-using-aws-datasync/
