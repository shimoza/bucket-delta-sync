"""Configuration loading.

Secrets are NEVER stored in the config file. The config names an environment
variable, and the real value is read from the environment at run time. This is
the load-bearing rule for a public, customer-synced repository: nothing secret
ever lands on disk in the repo or in shell history.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from typing import Optional


class ConfigError(Exception):
    pass


@dataclass
class SourceConfig:
    type: str
    options: dict = field(default_factory=dict)


@dataclass
class DestConfig:
    type: str
    options: dict = field(default_factory=dict)


@dataclass
class SyncConfig:
    compare: str = "key_size"
    delete: bool = True
    max_delete: str = "1%"
    threads: int = 16
    dry_run: bool = True
    state_dir: str = ".state"


@dataclass
class Config:
    source: SourceConfig
    dest: DestConfig
    sync: SyncConfig


def _require(d: dict, key: str, where: str) -> object:
    if key not in d:
        raise ConfigError(f"missing '{key}' in [{where}]")
    return d[key]


def _resolve_secret(env_name: str, label: str) -> str:
    """Read a secret from the environment by variable name.

    The config gives us the NAME of the env var, never the value. We refuse to
    proceed if the variable is unset, rather than silently trying anonymous
    access, so a misconfigured run fails loud instead of leaking or stalling.
    """
    if not env_name:
        raise ConfigError(f"{label}: no env var name configured")
    val = os.environ.get(env_name)
    if not val:
        raise ConfigError(
            f"{label}: environment variable '{env_name}' is not set. "
            f"Set it in your shell or a gitignored .env, never in the config file."
        )
    return val


def load_config(path: str) -> Config:
    with open(path, "rb") as f:
        raw = tomllib.load(f)

    src = raw.get("source", {})
    dst = raw.get("dest", {})
    syn = raw.get("sync", {})

    source = SourceConfig(type=str(_require(src, "type", "source")),
                          options={k: v for k, v in src.items() if k != "type"})
    dest = DestConfig(type=str(_require(dst, "type", "dest")),
                      options={k: v for k, v in dst.items() if k != "type"})
    sync = SyncConfig(
        compare=str(syn.get("compare", "key_size")),
        delete=bool(syn.get("delete", True)),
        max_delete=str(syn.get("max_delete", "1%")),
        threads=int(syn.get("threads", 16)),
        dry_run=bool(syn.get("dry_run", True)),
        state_dir=str(syn.get("state_dir", ".state")),
    )

    if sync.compare != "key_size":
        raise ConfigError(
            f"unsupported compare mode '{sync.compare}'. Only 'key_size' is "
            f"implemented (ETag and modtime are unreliable cross-cloud)."
        )
    return Config(source=source, dest=dest, sync=sync)


def resolve_secret(env_name: str, label: str) -> str:
    """Public wrapper so adapters resolve their own secrets the same safe way."""
    return _resolve_secret(env_name, label)


def parse_max_delete(spec: str, dest_count: int) -> int:
    """Turn a max_delete spec ('100', '5%') into an absolute object count.

    The cap is compared against the number of deletions a run would perform. A
    percentage is taken of the CURRENT destination object count, so an empty or
    tiny destination cannot be wiped by a single bad source listing.
    """
    spec = str(spec).strip()
    if spec.endswith("%"):
        pct = float(spec[:-1])
        if pct < 0:
            raise ConfigError("max_delete percentage cannot be negative")
        return int(dest_count * pct / 100.0)
    n = int(spec)
    if n < 0:
        raise ConfigError("max_delete count cannot be negative")
    return n
