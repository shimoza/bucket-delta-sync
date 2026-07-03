"""Sync engine: diff, then copy and delete to make the destination mirror.

Flow:
  1. One streaming diff pass over source + destination listings. The two
     listings are prefetched CONCURRENTLY (each in its own thread), so a pass
     costs max(listing times), not their sum. While diffing, tally both counts
     and write a copy plan and a delete plan to disk (JSON lines, gitignored -
     they contain raw object keys). O(1) memory regardless of object count.
  2. Apply the delete cap. If the plan would delete more than the cap allows,
     abort before writing anything. A source-side listing glitch must never be
     able to empty the destination.
  3. Dry-run (the default) stops here and prints the plan.
  4. Apply: copy phase (bounded-concurrency threads), then delete phase.

Resume model: there is deliberately NO cross-run checkpoint. Every object that
was copied successfully shows up in the destination listing, so the next diff
excludes it automatically - the diff IS the checkpoint. A crashed run is
resumed by simply re-running.

Source safety: the engine wraps the source adapter in a read-only proxy, so
destination-style calls (put_object, delete_keys) cannot even be expressed
against the source. The single gated exception is Archive rehydration
(start_restore), which mutates the source tier and therefore runs ONLY when
the config sets rehydrate = true; the default is false, meaning zero writes of
any kind against the source.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Iterable, Iterator, Optional

from .config import Config, parse_max_delete
from .diff import diff
from .models import COPY, DELETE, ObjectInfo
from .progress import Progress, human_size, human_time
from .adapters.base import DestAdapter, SourceAdapter

# Cold tiers across providers: Azure Archive is rehydratable by this tool
# (opt-in); S3-style cold classes are reported and skipped (no restore support).
_AZURE_ARCHIVE = "Archive"
COLD_TIERS = frozenset({"Archive", "GLACIER", "DEEP_ARCHIVE", "COLD"})


class DeleteCapExceeded(Exception):
    pass


class RunLockHeld(Exception):
    pass


@dataclass
class Plan:
    copy_plan_file: str
    delete_plan_file: str
    copy_count: int
    copy_bytes: int
    delete_count: int
    dest_count: int
    cold_count: int


@dataclass
class RunResult:
    plan: Plan
    applied: bool
    copied: int = 0
    copy_failed: int = 0
    deleted: int = 0
    delete_failed: int = 0

    @property
    def failed(self) -> int:
        return self.copy_failed + self.delete_failed


class _SourceReadOnly:
    """Read-only view of a source adapter.

    Exposes exactly the read operations plus the engine-gated restore hooks.
    Anything else (put_object, delete_keys, ...) raises AttributeError, so a
    bug or a mixed-up adapter can never mutate the source through the engine.
    """

    __slots__ = ("_a",)

    def __init__(self, adapter: SourceAdapter):
        object.__setattr__(self, "_a", adapter)

    def iter_objects(self):
        return self._a.iter_objects()

    def open_stream(self, key: str):
        return self._a.open_stream(key)

    def start_restore(self, key: str):
        return self._a.start_restore(key)

    def is_restored(self, key: str) -> bool:
        return self._a.is_restored(key)

    def close(self):
        return self._a.close()


class _RunLock:
    """One run per state_dir. Overlapping scheduled runs would clobber each
    other's plan files and double-execute deletes, so the second run refuses."""

    def __init__(self, state_dir: str):
        self._path = os.path.join(state_dir, ".lock")
        self._fp = None

    def acquire(self) -> None:
        try:
            import fcntl
        except ImportError:  # non-POSIX dev box: no locking, documented Linux target
            return
        self._fp = open(self._path, "w")
        try:
            fcntl.flock(self._fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._fp.close()
            self._fp = None
            raise RunLockHeld(
                f"another sync is already running against this state dir "
                f"({self._path}). Overlapping runs are refused."
            )

    def release(self) -> None:
        if self._fp is not None:
            self._fp.close()
            self._fp = None


class _Prefetch:
    """Pull an iterator on a background thread through a bounded queue.

    Lets the source and destination listings proceed concurrently: the diff
    consumes from two queues instead of interleaving blocking network calls.
    Exceptions on the listing thread are re-raised in the consumer.
    """

    _END = object()

    def __init__(self, iterable: Iterable[ObjectInfo], name: str, maxsize: int = 8192):
        self._q: queue.Queue = queue.Queue(maxsize=maxsize)
        self._exc: Optional[BaseException] = None
        self._t = threading.Thread(target=self._run, args=(iterable,),
                                   name=f"list-{name}", daemon=True)
        self._t.start()

    def _run(self, iterable):
        try:
            for item in iterable:
                self._q.put(item)
        except BaseException as e:  # noqa: BLE001 - carried to the consumer
            self._exc = e
        finally:
            self._q.put(self._END)

    def __iter__(self) -> Iterator[ObjectInfo]:
        while True:
            item = self._q.get()
            if item is self._END:
                if self._exc is not None:
                    raise self._exc
                return
            yield item


def build_plan(source, dest: DestAdapter, state_dir: str) -> Plan:
    os.makedirs(state_dir, exist_ok=True)
    copy_file = os.path.join(state_dir, "copy.plan")
    delete_file = os.path.join(state_dir, "delete.plan")

    src_count = [0]
    dst_count = [0]
    copy_count = copy_bytes = delete_count = cold_count = 0

    def _tally(it, counter):
        for obj in it:
            counter[0] += 1
            yield obj

    print("  diffing source vs destination (concurrent listings, sorted merge)...",
          flush=True)
    start = time.time()
    src_stream = _Prefetch(_tally(source.iter_objects(), src_count), "source")
    dst_stream = _Prefetch(_tally(dest.iter_objects(), dst_count), "dest")

    # Plan lines are JSON so keys containing tabs or newlines survive intact;
    # a corrupted plan line could otherwise delete the wrong object.
    with open(copy_file, "w", encoding="utf-8") as cf, \
         open(delete_file, "w", encoding="utf-8") as df:
        for action in diff(src_stream, dst_stream):
            if action.op == COPY:
                cf.write(json.dumps([action.key, action.size, action.tier,
                                     action.content_type]) + "\n")
                copy_count += 1
                copy_bytes += action.size
                if action.tier in COLD_TIERS:
                    cold_count += 1
            elif action.op == DELETE:
                df.write(json.dumps(action.key) + "\n")
                delete_count += 1

    print(f"  diff done in {human_time(time.time() - start)}: "
          f"source {src_count[0]:,}, dest {dst_count[0]:,} | "
          f"copy {copy_count:,} ({human_size(copy_bytes)}), "
          f"delete {delete_count:,}, cold-tier {cold_count:,}",
          flush=True)

    return Plan(copy_file, delete_file, copy_count, copy_bytes,
                delete_count, dst_count[0], cold_count)


def _read_copy_plan(path: str):
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                key, size, tier, ct = json.loads(line)
                yield key, size, tier, ct


def _read_delete_plan(path: str):
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def _copy_phase(source, dest: DestAdapter, plan: Plan, threads: int,
                rehydrate: bool) -> Progress:
    prog = Progress(plan.copy_count, plan.copy_bytes, label="copy")
    gate = threading.BoundedSemaphore(threads * 4)

    def copy_one(key: str, size: int, tier, ct):
        try:
            if tier in COLD_TIERS:
                if not (rehydrate and tier == _AZURE_ARCHIVE):
                    prog.record_failure(
                        key, f"skipped: cold tier {tier} (rehydrate=false; "
                             f"source left untouched)")
                    return
                try:
                    thawed = source.is_restored(key)
                except Exception as e:  # noqa: BLE001
                    prog.record_failure(key, f"restore check failed: {e}")
                    return
                if not thawed:
                    prog.record_failure(key, "still archived (thaw incomplete)")
                    return
            stream = source.open_stream(key)
            try:
                dest.put_object(key, stream, size, ct)
            finally:
                close = getattr(stream, "close", None)
                if close:
                    close()
            prog.record_success(size)
        except Exception as e:  # noqa: BLE001 - record and continue
            prog.record_failure(key, str(e))
        finally:
            gate.release()

    with ThreadPoolExecutor(max_workers=threads) as pool:
        for key, size, tier, ct in _read_copy_plan(plan.copy_plan_file):
            gate.acquire()
            pool.submit(copy_one, key, size, tier, ct)
        # pool context exit joins all workers; no mid-stream barrier.

    prog.final()
    return prog


def _thaw_archive(source, plan: Plan, threads: int, poll_interval: int = 60) -> None:
    """Rehydrate Azure Archive blobs before the copy phase.

    Only reached when config rehydrate=true. Every restore call is wrapped so a
    single failure (a blob deleted meanwhile, or a credential without set-tier
    permission) degrades to a reported skip instead of crashing the run.
    """
    archive_keys = [k for k, _s, t, _c in _read_copy_plan(plan.copy_plan_file)
                    if t == _AZURE_ARCHIVE]
    if not archive_keys:
        return
    print(f"  rehydrating {len(archive_keys):,} Archive-tier source blobs "
          f"(opt-in; permanent tier change on the source; can take hours)...",
          flush=True)
    failed: set[str] = set()

    def _restore(k):
        try:
            source.start_restore(k)
        except Exception as e:  # noqa: BLE001
            failed.add(k)
            print(f"    restore failed for {k}: {e}", flush=True)

    with ThreadPoolExecutor(max_workers=min(threads, 8)) as pool:
        list(pool.map(_restore, archive_keys))

    pending = set(archive_keys) - failed
    while pending:
        time.sleep(poll_interval)

        def _check(k):
            try:
                return k, source.is_restored(k)
            except Exception:  # noqa: BLE001
                return k, False

        with ThreadPoolExecutor(max_workers=min(threads, 8)) as pool:
            results = list(pool.map(_check, pending))
        pending = {k for k, ok in results if not ok}
        print(f"    thawed {len(archive_keys) - len(failed) - len(pending):,}/"
              f"{len(archive_keys):,}", flush=True)


def _delete_phase(dest: DestAdapter, plan: Plan, threads: int) -> Progress:
    prog = Progress(plan.delete_count, 0, label="delete")
    gate = threading.BoundedSemaphore(threads * 2)

    def flush(keys: list[str]):
        try:
            failures = dest.delete_keys(keys)
            failed_keys = {k for k, _ in failures}
            for k, err in failures:
                prog.record_failure(k, err)
            for k in keys:
                if k not in failed_keys:
                    prog.record_success()
        except Exception as e:  # noqa: BLE001
            for k in keys:
                prog.record_failure(k, str(e))
        finally:
            gate.release()

    with ThreadPoolExecutor(max_workers=threads) as pool:
        batch: list[str] = []
        for key in _read_delete_plan(plan.delete_plan_file):
            batch.append(key)
            if len(batch) >= 1000:
                gate.acquire()
                pool.submit(flush, batch)
                batch = []
        if batch:
            gate.acquire()
            pool.submit(flush, batch)

    prog.final()
    return prog


def run_sync(config: Config, source: SourceAdapter, dest: DestAdapter,
             apply: bool | None = None) -> RunResult:
    """Run one delta sync. Honors dry-run, the delete cap and the run lock.

    ``apply`` overrides config.sync.dry_run: apply=False forces a dry run,
    apply=True forces writes. When None, config decides.
    """
    do_write = (not config.sync.dry_run) if apply is None else apply

    # Structural guarantee: from here on the engine cannot express a mutating
    # destination-style call against the source.
    source = _SourceReadOnly(source)

    os.makedirs(config.sync.state_dir, exist_ok=True)
    lock = _RunLock(config.sync.state_dir)
    lock.acquire()
    try:
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
            print(f"  would delete: {plan.delete_count:,} objects (cap {cap:,})")
            if plan.cold_count:
                mode = ("will rehydrate (writes source tiers!)"
                        if config.sync.rehydrate else
                        "will be skipped and reported (rehydrate=false)")
                print(f"  cold-tier:    {plan.cold_count:,} objects — {mode}")
            print("  re-run with --apply to perform the sync.")
            return RunResult(plan=plan, applied=False)

        # --- apply -----------------------------------------------------------
        result = RunResult(plan=plan, applied=True)

        if plan.cold_count and config.sync.rehydrate:
            _thaw_archive(source, plan, config.sync.threads)

        if plan.copy_count:
            copy_prog = _copy_phase(source, dest, plan, config.sync.threads,
                                    config.sync.rehydrate)
            result.copied = copy_prog.ok
            result.copy_failed = copy_prog.failed

        if config.sync.delete and plan.delete_count:
            del_prog = _delete_phase(dest, plan, config.sync.threads)
            result.deleted = del_prog.ok
            result.delete_failed = del_prog.failed

        if result.failed:
            print(f"\n  sync finished WITH FAILURES: {result.copy_failed:,} copy, "
                  f"{result.delete_failed:,} delete. Re-run to retry; the diff "
                  f"picks up exactly what is still missing.")
        else:
            print("\n  sync complete.")
        return result
    finally:
        lock.release()
