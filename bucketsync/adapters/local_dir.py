"""Local filesystem adapter.

Mostly for testing and demos: it lets the full diff/copy/delete pipeline run
end to end without any cloud credentials, and it is a second concrete pair that
proves the adapter design generalizes. Keys are POSIX-style relative paths.

Note: it sorts the file list in memory, so it is not meant for the millions of
objects the cloud adapters stream. Use it for tests and small trees.
"""

from __future__ import annotations

import os
import shutil
from typing import BinaryIO, Iterable, Iterator

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
            items.append(ObjectInfo(key, os.path.getsize(full), None))
    items.sort(key=lambda o: o.key)
    yield from items


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

    def put_object(self, key: str, stream: BinaryIO, size: int) -> None:
        dest = os.path.join(self.root, key)
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        with open(dest, "wb") as f:
            shutil.copyfileobj(stream, f)

    def delete_keys(self, keys: Iterable[str]) -> None:
        for k in keys:
            path = os.path.join(self.root, k)
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
