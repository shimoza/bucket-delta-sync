"""Small value types shared across the package."""

from __future__ import annotations

from typing import NamedTuple, Optional


class ObjectInfo(NamedTuple):
    """One object as seen in a listing.

    ``key`` is the full object key (Azure blob name, S3 key). ``size`` is the
    byte length. ``tier`` is the storage/access tier when the backend exposes it
    ("Hot", "Cool", "Cold", "Archive", or None when unknown). Only ``key`` and
    ``size`` take part in the diff; ``tier`` drives the rehydrate-before-copy step.
    """

    key: str
    size: int
    tier: Optional[str] = None


# Diff actions. The diff is a stream of these, never a full in-memory list.
COPY = "copy"      # object is new in source, or its size differs -> (re)copy
DELETE = "delete"  # object exists only in destination -> remove it


class DiffAction(NamedTuple):
    op: str               # COPY or DELETE
    key: str
    size: int = 0         # source size for COPY, destination size for DELETE
    tier: Optional[str] = None
