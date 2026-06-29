"""Azure Blob Storage source adapter.

Read-only. Lists blobs (sorted by name, which Azure guarantees), streams their
bytes, and rehydrates Archive-tier blobs before they can be read. Credentials
come from an environment variable named in the config: a storage account key, or
a SAS token. Never the command line.

This adapter is implemented against the documented azure-storage-blob 12.x API.
It is the one piece that has not yet been run against a live Azure account in
this environment (no Azure credentials here). Verify with a small container
before a full run. The rest of the pipeline (diff, copy, delete, OBS dest) is
live-tested.
"""

from __future__ import annotations

from typing import BinaryIO, Iterator

from azure.storage.blob import ContainerClient, StandardBlobTier
from azure.storage.blob._generated.models import RehydratePriority

from ..config import resolve_secret
from ..models import ObjectInfo
from .base import SourceAdapter

_ARCHIVE = "Archive"


class _BlobReader:
    """Adapt an Azure StorageStreamDownloader to a read(n) file-like object.

    Built on .chunks() so it streams instead of loading the whole blob, and so
    it does not depend on the SDK's optional .read() method being present.
    """

    def __init__(self, downloader):
        self._chunks = downloader.chunks()
        self._buf = bytearray()
        self._eof = False

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            # read all remaining
            while not self._eof:
                self._fill()
            data = bytes(self._buf)
            self._buf = bytearray()
            return data
        while len(self._buf) < n and not self._eof:
            self._fill()
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def _fill(self) -> None:
        try:
            self._buf.extend(next(self._chunks))
        except StopIteration:
            self._eof = True

    def close(self) -> None:
        self._buf = bytearray()
        self._eof = True


class AzureBlobSource(SourceAdapter):
    def __init__(self, opts: dict):
        account = opts["account"]
        container = opts["container"]
        self.prefix = opts.get("prefix", "") or ""
        account_url = opts.get("account_url") or f"https://{account}.blob.core.windows.net"

        if opts.get("sas_env"):
            credential = resolve_secret(opts["sas_env"], "source SAS token")
        elif opts.get("key_env"):
            credential = resolve_secret(opts["key_env"], "source account key")
        else:
            raise ValueError("azure_blob source needs key_env or sas_env in config")

        self._client = ContainerClient(
            account_url=account_url,
            container_name=container,
            credential=credential,
            max_single_get_size=64 * 1024 * 1024,
            max_chunk_get_size=8 * 1024 * 1024,
        )

    def iter_objects(self) -> Iterator[ObjectInfo]:
        # list_blobs yields BlobProperties in lexicographic name order.
        for b in self._client.list_blobs(name_starts_with=self.prefix or None):
            tier = getattr(b, "blob_tier", None)
            yield ObjectInfo(b.name, b.size or 0, tier)

    def open_stream(self, key: str) -> BinaryIO:
        downloader = self._client.get_blob_client(key).download_blob(
            max_concurrency=1)
        return _BlobReader(downloader)  # type: ignore[return-value]

    # --- archive tier --------------------------------------------------------
    def needs_restore(self, info: ObjectInfo) -> bool:
        return info.tier == _ARCHIVE

    def start_restore(self, key: str) -> None:
        # Move the blob to an online tier. Rehydration runs server-side and can
        # take hours; the engine polls is_restored() before copying.
        self._client.get_blob_client(key).set_standard_blob_tier(
            StandardBlobTier.HOT,
            rehydrate_priority=RehydratePriority.standard,
        )

    def is_restored(self, key: str) -> bool:
        props = self._client.get_blob_client(key).get_blob_properties()
        # While thawing, archive_status is 'rehydrate-pending-to-hot/cool'.
        if props.archive_status:
            return False
        return getattr(props, "blob_tier", None) != _ARCHIVE

    def close(self) -> None:
        self._client.close()
