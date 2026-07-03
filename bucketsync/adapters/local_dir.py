"""Local filesystem adapter.

Mostly for testing and demos: it lets the full diff/copy/delete pipeline run
end to end without any cloud credentials, and it is a second concrete pair that
proves the adapter design generalizes. Keys are POSIX-style relative paths.

Note: it sorts the file list in memory, so it is not meant for the millions of
objects the cloud adapters stream. Use it for tests and small trees.
"""

from __future__ import annotations

import mimetypes
import os
import shutil
from typing import BinaryIO, Iterable, Iterator, Optional

from ..models import ObjectInfo
from .base import DestAdapter, SourceAdapter


def _walk_sorted(root: str, prefix: str) -> Iterator[ObjectInfo]:
    items: list[ObjectInfo] = []
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            full = os.path.join(dirpath, name)
            key = os.path.relpath(full, root).replace(os.sep, "/")
            if prefix and not key.startswith(prefix):
                continue
            ct = mimetypes.guess_type(name)[0]
            items.append(ObjectInfo(key, os.path.getsize(full), None, ct))
    items.sort(key=lambda o: o.key)
    yield from items


def _safe_join(root: str, key: str) -> str:
    """Join a key onto the root, refusing paths that escape it."""
    path = os.path.realpath(os.path.join(root, key))
    base = os.path.realpath(root)
    if path != base and not path.startswith(base + os.sep):
        raise ValueError(f"key escapes the destination root: {key!r}")
    return path


class LocalDirSource(SourceAdapter):
    def __init__(self, opts: dict):
        self.root = opts["path"]
        self.prefix = opts.get("prefix", "") or ""

    def iter_objects(self) -> Iterator[ObjectInfo]:
        yield from _walk_sorted(self.root, self.prefix)

    def open_stream(self, key: str) -> BinaryIO:
        return open(os.path.join(self.root, key), "rb")


class LocalDirDest(DestAdapter):
    def __init__(self, opts: dict):
        self.root = opts["path"]
        self.prefix = opts.get("prefix", "") or ""

    def iter_objects(self) -> Iterator[ObjectInfo]:
        if not os.path.isdir(self.root):
            return
        yield from _walk_sorted(self.root, self.prefix)

    def put_object(self, key: str, stream: BinaryIO, size: int,
                   content_type: Optional[str] = None) -> None:
        dest = _safe_join(self.root, key)
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        with open(dest, "wb") as f:
            shutil.copyfileobj(stream, f)

    def delete_keys(self, keys: Iterable[str]) -> list[tuple[str, str]]:
        failures: list[tuple[str, str]] = []
        for k in keys:
            try:
                os.remove(_safe_join(self.root, k))
            except FileNotFoundError:
                pass
            except (OSError, ValueError) as e:
                failures.append((k, str(e)))
        return failures
