"""Adapter interfaces.

A SourceAdapter is read-only: it lists objects (sorted by key) and streams their
bytes. The ONLY mutating source operation in the whole tool is Archive-tier
rehydration (start_restore), and the engine invokes it exclusively when the
operator sets ``rehydrate = true`` in the config; with the default (false) the
tool performs zero writes against the source. The engine additionally wraps the
source in a read-only proxy so destination-style calls (put/delete) cannot even
be expressed against it.

A DestAdapter lists, writes and deletes. The engine only ever talks to these two
interfaces, so adding a new cloud means writing one adapter, not touching the
engine.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import BinaryIO, Iterable, Iterator, Optional

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

    # --- archive / cold tier ---------------------------------------------------
    # start_restore MUTATES the source (in Azure it permanently re-tiers the
    # blob). The engine only calls it when config sync.rehydrate is true.
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
    def put_object(self, key: str, stream: BinaryIO, size: int,
                   content_type: Optional[str] = None) -> None:
        """Write one object from a readable stream, carrying its MIME type."""

    @abstractmethod
    def delete_keys(self, keys: Iterable[str]) -> list[tuple[str, str]]:
        """Delete a batch of keys. Returns per-key failures as (key, error)
        pairs; an empty list means every key was deleted. Implementations must
        not raise for individual key failures, only for transport-level errors
        that doom the whole batch."""

    def close(self) -> None:
        pass
