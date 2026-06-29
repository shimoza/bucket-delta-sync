"""Resumable checkpoint for the copy phase.

Adapted from the proven OBS cross-tenant tool. Completed object keys are stored
as short hashes, one per line, line-buffered so an abrupt kill still leaves a
valid file. On resume, already-copied objects are skipped with no API call.

Privacy note: object keys can embed patient identifiers, so we store a SHA-1
hash of the key, never the key itself. Checkpoint files are also gitignored.
"""

from __future__ import annotations

import hashlib
import os


class Checkpoint:
    def __init__(self, path: str):
        self.path = path
        self._done: set[str] = set()
        self._fp = None

    @staticmethod
    def _hash(key: str, size: int) -> str:
        # Hash key AND size, so a later run where the object changed size does
        # not falsely match an old checkpoint entry and skip the re-copy.
        return hashlib.sha1(f"{key}\0{size}".encode("utf-8")).hexdigest()

    def load(self) -> int:
        if os.path.exists(self.path):
            with open(self.path) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        self._done.add(line)
        self._fp = open(self.path, "a", buffering=1)
        return len(self._done)

    def is_done(self, key: str, size: int) -> bool:
        return self._hash(key, size) in self._done

    def mark_done(self, key: str, size: int) -> None:
        h = self._hash(key, size)
        if h not in self._done:
            self._done.add(h)
            if self._fp:
                self._fp.write(h + "\n")

    def count(self) -> int:
        return len(self._done)

    def close(self) -> None:
        if self._fp:
            self._fp.close()
            self._fp = None

    def clear(self) -> None:
        """Remove the checkpoint. Called after a clean run so the next run, a
        fresh delta, does not inherit stale completion marks."""
        self.close()
        self._done.clear()
        try:
            os.remove(self.path)
        except FileNotFoundError:
            pass
