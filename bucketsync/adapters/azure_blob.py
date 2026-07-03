"""Azure Blob Storage source adapter.

Read-only in normal operation: it lists blobs (sorted by name, which Azure
guarantees) and streams their bytes. The single mutating call it can make,
``start_restore`` (Set Blob Tier, a PERMANENT tier change on the source blob),
is invoked by the engine only when the operator sets ``rehydrate = true`` in the
config. With the default (false) this adapter issues zero writes, so a
container-scoped SAS with read+list permissions is fully sufficient.

Credentials come from an environment variable named in the config: a storage
account key, or a SAS token. Never the command line.
"""

from __future__ import annotations

from typing import BinaryIO, Iterator

from azure.storage.blob import ContainerClient, RehydratePriority, StandardBlobTier

from ..config import require_opt, resolve_secret
from ..models import ObjectInfo
from .base import SourceAdapter

_ARCHIVE = "Archive"


class _BlobReader:
    """Adapt an Azure StorageStreamDownloader to a read(n) file-like object.

    Built on .chunks() so it streams instead of loading the whole blob. Uses an
    offset cursor instead of trimming the buffer on every read: the buffer is
    compacted once per arriving chunk, not memmoved on each read() call.
    """

    def __init__(self, downloader):
        self._chunks = downloader.chunks()
        self._buf = b""
        self._pos = 0
        self._eof = False

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            parts = [self._buf[self._pos:]]
            for chunk in self._chunks:
                parts.append(chunk)
            self._buf = b""
            self._pos = 0
            self._eof = True
            return b"".join(parts)
        while (len(self._buf) - self._pos) < n and not self._eof:
            try:
                nxt = next(self._chunks)
            except StopIteration:
                self._eof = True
                break
            # compact once per chunk: carry the unread tail forward
            self._buf = self._buf[self._pos:] + nxt
            self._pos = 0
        out = self._buf[self._pos:self._pos + n]
        self._pos += len(out)
        if self._pos >= len(self._buf):
            self._buf = b""
            self._pos = 0
        return out

    def close(self) -> None:
        self._buf = b""
        self._pos = 0
        self._eof = True


class AzureBlobSource(SourceAdapter):
    def __init__(self, opts: dict):
        account = require_opt(opts, "account", "source")
        container = require_opt(opts, "container", "source")
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
        # list_blobs yields BlobProperties in lexicographic name order, and each
        # item already carries ContentSettings - the content type costs nothing
        # extra here and is REQUIRED downstream so images/PDFs keep their MIME
        # type on the destination (clients download directly via signed URLs).
        for b in self._client.list_blobs(name_starts_with=self.prefix or None):
            tier = getattr(b, "blob_tier", None)
            ct = b.content_settings.content_type if b.content_settings else None
            yield ObjectInfo(b.name, b.size or 0, tier, ct)

    def open_stream(self, key: str) -> BinaryIO:
        downloader = self._client.get_blob_client(key).download_blob(
            max_concurrency=1)
        return _BlobReader(downloader)  # type: ignore[return-value]

    # --- archive tier (engine-gated: only called when config rehydrate=true) --
    def start_restore(self, key: str) -> None:
        # Move the blob to an online tier. In Azure this is a PERMANENT tier
        # change on the source (billing impact incl. archive early-deletion
        # fees). Rehydration runs server-side and can take hours; the engine
        # polls is_restored() before copying.
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
