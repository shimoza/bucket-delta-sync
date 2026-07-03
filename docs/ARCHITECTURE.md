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

## Decision: custom tool on the cloud SDKs

The engine is a custom Python tool (option 2), not an rclone wrapper. Reasons:

- It owns the diff, the resume semantics, the delete cap, and the archive
  rehydration outright, so the safety behaviour is in our code and testable, not
  spread across rclone flags.
- It does not depend on rclone reading Archive-tier source blobs, which rclone
  cannot do (409, no auto-rehydrate). The custom adapter rehydrates first.
- The diff and copy/delete engine is generalized behind a small adapter
  interface, so the source can be Azure Blob, an S3 store, or a local directory,
  and new pairs are one adapter each. The Azure source adapter is the only
  genuinely new piece versus the prior OBS-to-OBS tool.

rclone stays the documented fallback for an all-Hot source where minimal
maintenance matters more than control. The trade-off (rclone hardened multipart
and retry vs our own) is real, so the code reuses the proven retry, multipart
and progress patterns from the earlier OBS sync rather than inventing
them.

### Implementation map

| Concern | Where |
|---|---|
| Sorted-merge true-diff (key + size, streaming) | `bucketsync/diff.py` |
| Orchestration: plan, cap, dry-run, copy, delete | `bucketsync/engine.py` |
| Azure Blob source (list, stream, rehydrate) | `bucketsync/adapters/azure_blob.py` |
| S3 / OBS store (list, write, batch delete) | `bucketsync/adapters/s3_store.py` |
| Local dir (tests, demos, second pair) | `bucketsync/adapters/local_dir.py` |
| Read-only source proxy + run lock | `bucketsync/engine.py` |
| Config + env-var secret resolution | `bucketsync/config.py` |
| CLI (dry-run default, --apply to write) | `bucketsync/cli.py` |

### Verification status

Live-tested against TCP OBS (eu-de): diff, dry-run, apply (copy), delete
propagation, re-run resume, and the delete-cap abort all confirmed on a
throwaway bucket. The diff and full engine path are covered by offline tests
(`tests/`). The Azure source adapter is written to the azure-storage-blob 12.x
API but not yet run against a live Azure account (no Azure credentials in the
build environment). Verify it against a small container, and confirm the source
tier mix, before a full production run.

This document is updated as the tool evolves.

## Sources

- rclone Azure Blob backend and archive limitation: https://rclone.org/azureblob/
- rclone sync, delete cap and backup dir: https://rclone.org/commands/rclone_sync/
- rclone S3 providers, including HuaweiOBS: https://rclone.org/s3/
- rclone listing and memory at scale: https://rclone.org/docs/
- azcopy, S3 as source to Azure only: https://learn.microsoft.com/en-us/azure/storage/common/storage-use-azcopy-s3
- AWS DataSync Azure Blob to Amazon S3: https://aws.amazon.com/blogs/storage/migrating-azure-blob-storage-to-amazon-s3-using-aws-datasync/

## Source safety model

The source must never be modified: in the primary use case it is the only copy
of live production data while the mirror runs for weeks.

1. The engine wraps the source adapter in a read-only proxy. Destination-style
   calls (put, delete) do not exist on it, so no engine bug can mutate the
   source through the adapter.
2. The one mutating operation that exists at all, Azure Archive rehydration
   (a permanent Set-Blob-Tier on the source), runs only when the operator sets
   `rehydrate = true`. The default is false: archive objects are skipped and
   reported, and the run continues.
3. A tripwire test runs a full mirror, deletes included, against a source whose
   mutating methods raise; it asserts zero such calls happen.
4. The recommended source credential is a container-scoped SAS with read+list
   only, which makes source writes impossible at the credential level, below
   the software.
5. On the destination side: the delete cap bounds any single run, mismatched
   source/dest prefixes with deletes enabled are refused at config load, and
   mirroring a bucket onto itself is refused.

## Resume model (why there is no checkpoint file)

Every successfully copied object appears in the destination listing, so the
next run's diff excludes it automatically. The diff IS the checkpoint: resume
after any crash or interrupt is "run it again". A persistent checkpoint file
would duplicate that at the cost of RAM (about 1 GB at 6.2M objects) and a
stale-skip hazard (an object deleted and re-created between runs could be
wrongly skipped). Removing it made the seed-scale memory footprint flat.

## Performance notes (measured, 100k-object test set)

Measured on a 100k-object set with both sides populated (the steady-state
mirror pass): 74 s wall for both listings plus the diff, peak RSS 165 MB.
Listing throughput is ~1,300 items/s per side, bounded by the Azure SDK's XML
parsing, and the two listings overlap, so the pass costs the slower side only.
Linear extrapolation to 6.2M objects: roughly 80-90 minutes per pass at flat
memory. Tiny-object copy improved ~2.6x over the pre-optimization baseline
from the same host (transfer-manager overhead removed); in-region hosts gain
more, and ~2 MB objects run bandwidth-bound, not overhead-bound.

- Listings of source and destination run concurrently (background prefetch
  threads feeding the sorted merge), so a pass costs max(listing times), not
  the sum.
- Sub-64MB objects (statistically all of the wound-photo workload) go through
  a single `put_object` call with an in-memory body: no per-object
  TransferManager, and retries are safe because the body is bytes.
- The copy and delete phases use bounded-semaphore submission with no
  mid-stream barrier, so one slow object does not stall the pipeline.
- Plan files are JSON-lines (keys with tabs/newlines survive) with normal
  buffering; they are the only disk state and are gitignored (keys can embed
  patient identifiers).
