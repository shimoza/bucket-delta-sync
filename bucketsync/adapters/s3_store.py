"""S3-compatible adapter (Amazon S3, MinIO, and TCP OBS).

Works as a destination (list, write, delete) and, for S3-to-S3 pairs, as a
source (list, stream). Credentials are read from environment variables named in
the config, never passed on the command line.

OBS specifics: endpoint ``https://obs.eu-de.otc.t-systems.com``, region
``eu-de``, SigV4, virtual-hosted addressing. We never auto-create the bucket
(OBS rejects the implicit create) and never compare by ETag (multipart / SSE
ETags are not MD5). Note: do not point the destination at a WORM (Object Lock)
bucket - OBS's ListObjectsV2 is known to paginate forever on those; the diff's
sorted-order guard turns that into a loud error instead of an infinite loop.
"""

from __future__ import annotations

from typing import BinaryIO, Iterable, Iterator, Optional

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

from ..config import require_opt, resolve_secret
from ..models import ObjectInfo
from .base import DestAdapter, SourceAdapter

_DELETE_BATCH = 1000  # S3/OBS hard limit per delete_objects call
_MB = 1024 * 1024
_MULTIPART_THRESHOLD = 64 * _MB


class S3Store(SourceAdapter, DestAdapter):
    def __init__(self, opts: dict):
        self.bucket = require_opt(opts, "bucket", "s3 store")
        self.prefix = opts.get("prefix", "") or ""
        endpoint = opts.get("endpoint")
        region = opts.get("region", "eu-de")
        ak_env = require_opt(opts, "access_key_env", "s3 store")
        sk_env = require_opt(opts, "secret_key_env", "s3 store")
        access_key = resolve_secret(ak_env, f"s3 access key ({ak_env})")
        secret_key = resolve_secret(sk_env, f"s3 secret key ({sk_env})")

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
                # Cover the engine's worker threads; the default pool of 10
                # would recycle TLS connections under 16-32 threads.
                max_pool_connections=64,
                # boto3 >= 1.36 defaults to CRC32 "flexible checksums", which
                # wrap the body in aws-chunked transfer encoding with a trailer.
                # OBS (and other non-AWS S3 stores) store that framing as part of
                # the object, inflating every upload by ~40 bytes and corrupting
                # it. Force the classic behaviour so the body is sent verbatim.
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
            ),
        )
        # Only objects >= 64 MB take the multipart path; per-object concurrency
        # stays low because the engine already parallelises across objects.
        self._transfer = TransferConfig(
            multipart_threshold=_MULTIPART_THRESHOLD,
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
                # S3 listings do not include Content-Type (a HEAD per object
                # would be needed), so content_type stays None for S3 sources.
                yield ObjectInfo(obj["Key"], obj["Size"],
                                 obj.get("StorageClass"))

    # --- source side ---------------------------------------------------------
    def open_stream(self, key: str) -> BinaryIO:
        resp = self._client.get_object(Bucket=self.bucket, Key=key)
        return resp["Body"]  # StreamingBody is a readable file-like

    # --- destination side ----------------------------------------------------
    def put_object(self, key: str, stream: BinaryIO, size: int,
                   content_type: Optional[str] = None) -> None:
        if size < _MULTIPART_THRESHOLD:
            # Fast path for the overwhelmingly common case (~2 MB objects):
            # one put_object call with an in-memory body. No per-object
            # TransferManager (which costs three thread pools per call), and
            # the bytes body makes botocore's retries safe.
            body = stream.read()
            extra = {"ContentType": content_type} if content_type else {}
            self._client.put_object(Bucket=self.bucket, Key=key, Body=body,
                                    **extra)
        else:
            extra_args = {"ContentType": content_type} if content_type else None
            self._client.upload_fileobj(stream, self.bucket, key,
                                        Config=self._transfer,
                                        ExtraArgs=extra_args)

    def delete_keys(self, keys: Iterable[str]) -> list[tuple[str, str]]:
        failures: list[tuple[str, str]] = []
        batch: list[str] = []
        for k in keys:
            batch.append(k)
            if len(batch) >= _DELETE_BATCH:
                failures.extend(self._delete_batch(batch))
                batch = []
        if batch:
            failures.extend(self._delete_batch(batch))
        return failures

    def _delete_batch(self, batch: list[str]) -> list[tuple[str, str]]:
        try:
            resp = self._client.delete_objects(
                Bucket=self.bucket,
                Delete={"Objects": [{"Key": k} for k in batch], "Quiet": True},
            )
        except ClientError as e:
            # Transport/auth failure dooms the whole batch, but not the run.
            msg = str(e)
            return [(k, msg) for k in batch]
        # Per-key errors: only the listed keys failed, the rest are deleted.
        return [(err.get("Key", "?"),
                 f"{err.get('Code', '?')}: {err.get('Message', '')}")
                for err in (resp.get("Errors") or [])]
