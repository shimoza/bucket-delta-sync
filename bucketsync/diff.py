"""Sorted-merge true diff between a source and a destination listing.

Both listings must arrive sorted by key in ascending byte order. S3
``list_objects_v2`` and Azure ``list_blobs`` both return keys in lexicographic
order, so we can walk the two streams in parallel and decide each key in O(1)
memory, no full key set in RAM. This is what lets the diff scale to millions of
objects.

Comparison is key + size (see docs/ARCHITECTURE.md for why not ETag or modtime):
  - key in source only            -> COPY (new object)
  - key in destination only       -> DELETE (orphan, removed at source)
  - same key, different size      -> COPY (content changed)
  - same key, same size           -> match, no action

We assert each stream is non-decreasing. A backend that returns an unsorted
listing would silently corrupt the diff, so we fail loud instead.
"""

from __future__ import annotations

from typing import Iterable, Iterator

from .models import COPY, DELETE, DiffAction, ObjectInfo


def _checked(stream: Iterable[ObjectInfo], side: str) -> Iterator[ObjectInfo]:
    prev = None
    for obj in stream:
        if prev is not None and obj.key < prev:
            raise ValueError(
                f"{side} listing is not sorted ascending: "
                f"{prev!r} came before {obj.key!r}. The sorted-merge diff "
                f"requires sorted input."
            )
        prev = obj.key
        yield obj


def diff(source: Iterable[ObjectInfo],
         destination: Iterable[ObjectInfo]) -> Iterator[DiffAction]:
    """Yield COPY and DELETE actions to make destination mirror source."""
    src = _checked(source, "source")
    dst = _checked(destination, "destination")

    s = next(src, None)
    d = next(dst, None)

    while s is not None or d is not None:
        if s is not None and (d is None or s.key < d.key):
            yield DiffAction(COPY, s.key, s.size, s.tier)
            s = next(src, None)
        elif d is not None and (s is None or d.key < s.key):
            yield DiffAction(DELETE, d.key, d.size)
            d = next(dst, None)
        else:
            # same key on both sides
            if s.size != d.size:
                yield DiffAction(COPY, s.key, s.size, s.tier)
            s = next(src, None)
            d = next(dst, None)
