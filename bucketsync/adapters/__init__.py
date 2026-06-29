"""Adapter registry and factory.

Each object store is a SourceAdapter (read + list) or a DestAdapter (list +
write + delete). Pairs are added by writing a new adapter and registering it
here, which keeps the engine independent of any specific cloud.
"""

from __future__ import annotations

from ..config import DestConfig, SourceConfig
from .base import DestAdapter, SourceAdapter


def make_source(cfg: SourceConfig) -> SourceAdapter:
    if cfg.type == "azure_blob":
        from .azure_blob import AzureBlobSource
        return AzureBlobSource(cfg.options)
    if cfg.type == "local_dir":
        from .local_dir import LocalDirSource
        return LocalDirSource(cfg.options)
    if cfg.type == "s3":
        from .s3_store import S3Store
        return S3Store(cfg.options)
    raise ValueError(f"unknown source type '{cfg.type}'")


def make_dest(cfg: DestConfig) -> DestAdapter:
    if cfg.type == "s3":
        from .s3_store import S3Store
        return S3Store(cfg.options)
    if cfg.type == "local_dir":
        from .local_dir import LocalDirDest
        return LocalDirDest(cfg.options)
    raise ValueError(f"unknown dest type '{cfg.type}'")


__all__ = ["SourceAdapter", "DestAdapter", "make_source", "make_dest"]
