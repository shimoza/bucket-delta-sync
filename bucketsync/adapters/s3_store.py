"""S3-compatible adapter (Amazon S3, MinIO, and TCP OBS).

Works as a destination (list, write, delete) and, for S3-to-S3 pairs, as a
source (list, stream). Credentials are read from environment variables named in
the config, never passed on the command line.

OBS specifics: endpoint ``https://obs.eu-de.otc.t-systems.com``, region
``eu-de``, SigV4, virtual-hosted addressing. We never auto-create the bucket
(OBS rejects the implicit create) and never compare by ETag (multipart / SSE
ETags are not MD5).
"""

from __future__ import annotations

from typing import BinaryIO, Iterable, Iterator

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config as BotoConfig

from ..config import resolve_secret
from ..models import ObjectInfo
from .base import DestAdapter, SourceAdapter

_DELETE_BATCH = 1000  # S3/OBS hard limit per delete_objects call
_MB = 1024 * 1024


class S3Store(SourceAdapter, DestAdapter):
    def __init__(self, opts: dict):
        self.bucket = opts["bucket"]
        self.prefix = opts.get("prefix", "") or ""
        endpoint = opts.get("endpoint")
        region = opts.get("region", "eu-de")
        access_key = resolve_secret(opts["access_key_env"], "dest access key")
        secret_key = resolve_secret(opts["secret_key_env"], "dest secret key")

        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            region_name=region,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            config=BotoConfig(
                signature_version="s3v4",
                s3={"addressing_style": "virtual"},
                retries={"max_attempts": 5, "mode": "standard"},
                # boto3 >= 1.36 defaults to CRC32 "flexible checksums", which
                # wrap the body in aws-chunked transfer encoding with a trailer.
                # OBS (and other non-AWS S3 stores) store that framing as part of
                # the object, inflating every upload by ~40 bytes and corrupting
                # it. Force the classic behaviour so the body is sent verbatim.
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
            ),
        )
        # We already parallelize across objects, so keep per-object multipart
        # concurrency low. 64 MB threshold keeps the avg 2 MB object single-part.
        self._transfer = TransferConfig(
            multipart_threshold=64 * _MB,
            multipart_chunksize=64 * _MB,
            max_concurrency=4,
            use_threads=True,
        )

    # --- listing (sorted by key, as S3 guarantees) ---------------------------
    def iter_objects(self) -> Iterator[ObjectInfo]:
        paginator = self._client.get_paginator("list_objects_v2")
        kwargs = {"Bucket": self.bucket}
        if self.prefix:
            kwargs["Prefix"] = self.prefix
        for page in paginator.paginate(**kwargs):
            for obj in page.get("Contents", []):
                yield ObjectInfo(obj["Key"], obj["Size"],
                                 obj.get("StorageClass"))

    # --- source side ---------------------------------------------------------
    def open_stream(self, key: str) -> BinaryIO:
        resp = self._client.get_object(Bucket=self.bucket, Key=key)
        return resp["Body"]  # StreamingBody is a readable file-like

    # --- destination side ----------------------------------------------------
    def put_object(self, key: str, stream: BinaryIO, size: int) -> None:
        # Default bucket SSE-KMS handles encryption, so no SSE args here.
        self._client.upload_fileobj(stream, self.bucket, key,
                                    Config=self._transfer)

    def delete_keys(self, keys: Iterable[str]) -> None:
        batch: list[dict] = []
        for k in keys:
            batch.append({"Key": k})
            if len(batch) >= _DELETE_BATCH:
                self._flush_delete(batch)
                batch = []
        if batch:
            self._flush_delete(batch)

    def _flush_delete(self, batch: list[dict]) -> None:
        resp = self._client.delete_objects(
            Bucket=self.bucket,
            Delete={"Objects": batch, "Quiet": True},
        )
        errs = resp.get("Errors") or []
        if errs:
            first = errs[0]
            raise RuntimeError(
                f"delete failed for {len(errs)} keys, first: "
                f"{first.get('Key')} -> {first.get('Code')} {first.get('Message')}"
            )
