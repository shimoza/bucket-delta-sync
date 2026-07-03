"""Thread-safe progress reporting: objects/s, throughput, ETA."""

from __future__ import annotations

import re
import threading
import time

# SDK exceptions can embed full request URLs, including SAS signatures or
# presigned-URL credentials. Failure lines go to operator logs, so scrub any
# signature-shaped query parameter before recording.
_SECRET_QS = re.compile(r"((?:sig|Signature|X-Amz-Signature|AccessKeyId|"
                        r"X-Amz-Credential)=)[^&\s\"']+", re.IGNORECASE)


def redact(text: str) -> str:
    return _SECRET_QS.sub(r"\1REDACTED", text)


def human_size(b: float) -> str:
    for u in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(b) < 1024:
            return f"{b:.1f} {u}"
        b /= 1024
    return f"{b:.1f} EB"


def human_time(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds // 60:.0f}m {seconds % 60:.0f}s"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    return f"{h}h {m}m"


class Progress:
    def __init__(self, total_objects: int, total_bytes: int, label: str = "",
                 report_interval: float = 5.0):
        self.total_objects = total_objects
        self.total_bytes = total_bytes
        self.label = label
        self.interval = report_interval
        self.ok = 0
        self.failed = 0
        self.skipped = 0
        self.bytes_done = 0
        self.start = time.time()
        self._last = self.start
        self._lock = threading.Lock()
        self.failures: list[tuple[str, str]] = []

    def record_success(self, size: int = 0) -> None:
        with self._lock:
            self.ok += 1
            self.bytes_done += size
            self._maybe_report()

    def record_failure(self, key: str, error: str) -> None:
        with self._lock:
            self.failed += 1
            if len(self.failures) < 100:
                self.failures.append((key, redact(error)))
            self._maybe_report()

    def record_skip(self, size: int = 0) -> None:
        with self._lock:
            self.skipped += 1
            self._maybe_report()

    def _maybe_report(self) -> None:
        now = time.time()
        if now - self._last >= self.interval:
            self._print()
            self._last = now

    def _print(self) -> None:
        done = self.ok + self.failed + self.skipped
        elapsed = max(time.time() - self.start, 0.001)
        rate = done / elapsed
        thr = self.bytes_done / elapsed
        eta = (self.total_objects - done) / rate if rate > 0 else 0
        print(f"    [{self.label}] {done:,}/{self.total_objects:,} "
              f"({rate:.0f} obj/s, {human_size(thr)}/s) "
              f"ok={self.ok:,} skip={self.skipped:,} fail={self.failed:,} "
              f"ETA {human_time(eta)}", flush=True)

    def final(self) -> None:
        elapsed = max(time.time() - self.start, 0.001)
        print(f"    [{self.label}] done in {human_time(elapsed)}: "
              f"ok={self.ok:,} skip={self.skipped:,} fail={self.failed:,} "
              f"({human_size(self.bytes_done)})", flush=True)
        if self.failures:
            print(f"    first {len(self.failures)} failures:", flush=True)
            for key, err in self.failures[:10]:
                print(f"      {key}: {err}", flush=True)
