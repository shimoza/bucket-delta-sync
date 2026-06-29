"""Sync engine: diff, then copy and delete to make the destination mirror.

Flow:
  1. One streaming diff pass over source + destination listings. While passing,
     tally both counts and write a copy plan and a delete plan to disk (TSVs of
     keys, gitignored). O(1) memory regardless of object count.
  2. Apply the delete cap. If the plan would delete more than the cap allows,
     abort before writing anything. A source-side listing glitch must never be
     able to empty the destination.
  3. Dry-run (the default) stops here and prints the plan.
  4. Apply: copy phase (threaded, resumable, rehydrate Archive first), then
     delete phase (threaded batch).
"""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from .checkpoint import Checkpoint
from .config import Config, parse_max_delete
from .diff import diff
from .models import COPY, DELETE
from .progress import Progress, human_size, human_time
from .adapters.base import DestAdapter, SourceAdapter


@dataclass
class Plan:
    copy_keys_file: str
    delete_keys_file: str
    copy_count: int
    copy_bytes: int
    delete_count: int
    dest_count: int
    archive_count: int


class DeleteCapExceeded(Exception):
    pass


def _tally(it, counter: list[int]):
    for obj in it:
        counter[0] += 1
        yield obj


def build_plan(source: SourceAdapter, dest: DestAdapter, state_dir: str) -> Plan:
    os.makedirs(state_dir, exist_ok=True)
    copy_file = os.path.join(state_dir, "copy.tsv")
    delete_file = os.path.join(state_dir, "delete.tsv")

    src_count = [0]
    dst_count = [0]
    copy_count = copy_bytes = delete_count = archive_count = 0

    print("  diffing source vs destination (sorted merge)...", flush=True)
    start = time.time()
    with open(copy_file, "w", buffering=1) as cf, \
         open(delete_file, "w", buffering=1) as df:
        for action in diff(_tally(source.iter_objects(), src_count),
                           _tally(dest.iter_objects(), dst_count)):
            if action.op == COPY:
                cf.write(f"{action.key}\t{action.size}\t{action.tier or ''}\n")
                copy_count += 1
                copy_bytes += action.size
                if action.tier == "Archive":
                    archive_count += 1
            elif action.op == DELETE:
                df.write(f"{action.key}\n")
                delete_count += 1

    print(f"  diff done in {human_time(time.time() - start)}: "
          f"source {src_count[0]:,}, dest {dst_count[0]:,} | "
          f"copy {copy_count:,} ({human_size(copy_bytes)}), "
          f"delete {delete_count:,}, archive-to-thaw {archive_count:,}",
          flush=True)

    return Plan(copy_file, delete_file, copy_count, copy_bytes,
                delete_count, dst_count[0], archive_count)


def _read_copy_plan(path: str):
    with open(path) as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            key = parts[0]
            size = int(parts[1]) if len(parts) > 1 and parts[1] else 0
            tier = parts[2] if len(parts) > 2 and parts[2] else None
            yield key, size, tier


def _copy_phase(source: SourceAdapter, dest: DestAdapter, plan: Plan,
                threads: int, checkpoint: Checkpoint) -> Progress:
    prog = Progress(plan.copy_count, plan.copy_bytes, label="copy")

    def copy_one(key: str, size: int, tier: str | None):
        if checkpoint.is_done(key, size):
            prog.record_skip(size)
            return
        try:
            # Archive blobs are thawed in the wave step before this phase. Guard
            # against a straggler that is still frozen rather than 409-ing.
            if tier == "Archive" and not source.is_restored(key):
                prog.record_failure(key, "still in Archive tier (not thawed)")
                return
            stream = source.open_stream(key)
            try:
                dest.put_object(key, stream, size)
            finally:
                close = getattr(stream, "close", None)
                if close:
                    close()
            checkpoint.mark_done(key, size)
            prog.record_success(size)
        except Exception as e:  # noqa: BLE001 - record and continue
            prog.record_failure(key, str(e))

    with ThreadPoolExecutor(max_workers=threads) as pool:
        futures = []
        for key, size, tier in _read_copy_plan(plan.copy_keys_file):
            futures.append(pool.submit(copy_one, key, size, tier))
            if len(futures) >= threads * 4:
                for fut in as_completed(futures):
                    fut.result()
                futures = []
        for fut in as_completed(futures):
            fut.result()

    prog.final()
    return prog


def _thaw_archive(source: SourceAdapter, plan: Plan, poll_interval: int = 60):
    """Kick off rehydration for Archive-tier objects and wait for them.

    Simple wave: submit all restores, then poll until every one is online. For
    large archive sets a streaming wave (copy each as it thaws) is better; this
    keeps the first version clear. If the source is all-Hot this is a no-op.
    """
    archive_keys = [k for k, _s, t in _read_copy_plan(plan.copy_keys_file)
                    if t == "Archive"]
    if not archive_keys:
        return
    print(f"  rehydrating {len(archive_keys):,} Archive-tier objects "
          f"(hours, server-side)...", flush=True)
    for k in archive_keys:
        source.start_restore(k)
    pending = set(archive_keys)
    while pending:
        time.sleep(poll_interval)
        still = {k for k in pending if not source.is_restored(k)}
        done = len(pending) - len(still)
        print(f"    thawed {len(archive_keys) - len(still):,}/"
              f"{len(archive_keys):,}", flush=True)
        pending = still


def _delete_phase(dest: DestAdapter, plan: Plan, threads: int) -> Progress:
    prog = Progress(plan.delete_count, 0, label="delete")
    batch: list[str] = []

    def flush(keys: list[str]):
        try:
            dest.delete_keys(keys)
            for _ in keys:
                prog.record_success()
        except Exception as e:  # noqa: BLE001
            for k in keys:
                prog.record_failure(k, str(e))

    with ThreadPoolExecutor(max_workers=threads) as pool:
        futures = []
        with open(plan.delete_keys_file) as f:
            for line in f:
                key = line.rstrip("\n")
                if not key:
                    continue
                batch.append(key)
                if len(batch) >= 1000:
                    futures.append(pool.submit(flush, batch))
                    batch = []
        if batch:
            futures.append(pool.submit(flush, batch))
        for fut in as_completed(futures):
            fut.result()

    prog.final()
    return prog


def run_sync(config: Config, source: SourceAdapter, dest: DestAdapter,
             apply: bool | None = None) -> Plan:
    """Run one delta sync. Returns the Plan. Honors dry-run and the delete cap.

    ``apply`` overrides config.sync.dry_run: apply=False forces a dry run,
    apply=True forces writes. When None, config decides.
    """
    do_write = (not config.sync.dry_run) if apply is None else apply

    plan = build_plan(source, dest, config.sync.state_dir)

    cap = parse_max_delete(config.sync.max_delete, plan.dest_count)
    if config.sync.delete and plan.delete_count > cap:
        raise DeleteCapExceeded(
            f"would delete {plan.delete_count:,} objects but the cap is "
            f"{cap:,} (max_delete={config.sync.max_delete} of "
            f"{plan.dest_count:,} dest objects). Aborting. Raise max_delete "
            f"only if this is expected."
        )

    if not do_write:
        print("\n  DRY RUN — no changes written.")
        print(f"  would copy:   {plan.copy_count:,} objects "
              f"({human_size(plan.copy_bytes)})")
        print(f"  would delete: {plan.delete_count:,} objects "
              f"(cap {cap:,})")
        print("  re-run with --apply to perform the sync.")
        return plan

    # --- apply ---------------------------------------------------------------
    checkpoint = Checkpoint(os.path.join(config.sync.state_dir, "copied.checkpoint"))
    resumed = checkpoint.load()
    if resumed:
        print(f"  resuming: {resumed:,} objects already copied (checkpoint)")

    if plan.archive_count:
        _thaw_archive(source, plan)

    copy_failed = 0
    if plan.copy_count:
        copy_prog = _copy_phase(source, dest, plan, config.sync.threads, checkpoint)
        copy_failed = copy_prog.failed

    if config.sync.delete and plan.delete_count:
        _delete_phase(dest, plan, config.sync.threads)

    # Clear the checkpoint only on a clean copy phase. If anything failed, keep
    # it so a re-run resumes and retries just the failures.
    if copy_failed == 0:
        checkpoint.clear()
    else:
        checkpoint.close()
        print(f"  NOTE: {copy_failed:,} objects failed to copy. Re-run to "
              f"retry them (checkpoint kept).")

    print("\n  sync complete.")
    return plan
