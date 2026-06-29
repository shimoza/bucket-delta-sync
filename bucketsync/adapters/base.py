"""Adapter interfaces.

A SourceAdapter is read-only: it lists objects (sorted by key) and streams their
bytes, plus optional archive-rehydration hooks. A DestAdapter lists, writes and
deletes. The engine only ever talks to these two interfaces, so adding a new
cloud means writing one adapter, not touching the engine.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import BinaryIO, Iterable, Iterator

from ..models import ObjectInfo


class SourceAdapter(ABC):
    @abstractmethod
    def iter_objects(self) -> Iterator[ObjectInfo]:
        """Yield every object, sorted by key ascending."""

    @abstractmethod
    def open_stream(self, key: str) -> BinaryIO:
        """Return a binary, readable file-like object for the object's bytes.

        The caller closes it. Implementations should stream, not buffer the whole
        object, so large objects do not blow up memory.
        """

    # --- archive / cold tier (default: nothing is archived) ------------------
    def needs_restore(self, info: ObjectInfo) -> bool:
        return False

    def start_restore(self, key: str) -> None:  # pragma: no cover - backend specific
        raise NotImplementedError("this source has no archive tier to restore")

    def is_restored(self, key: str) -> bool:
        return True

    def close(self) -> None:
        pass


class DestAdapter(ABC):
    @abstractmethod
    def iter_objects(self) -> Iterator[ObjectInfo]:
        """Yield every object, sorted by key ascending.

        The engine tallies the destination count during this single pass and
        uses it to size the delete-cap percentage, so there is no separate
        (expensive) count call over millions of objects.
        """

    @abstractmethod
    def put_object(self, key: str, stream: BinaryIO, size: int) -> None:
        """Write one object from a readable stream."""

    @abstractmethod
    def delete_keys(self, keys: Iterable[str]) -> None:
        """Delete a batch of keys."""

    def close(self) -> None:
        pass
