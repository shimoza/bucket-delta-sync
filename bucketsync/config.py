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
    # Rehydrate Archive-tier source blobs before copying. OFF by default because
    # rehydration is a WRITE to the source (a permanent tier change in Azure).
    # With the default False, the tool performs zero mutating calls against the
    # source, and a read+list credential is fully sufficient.
    rehydrate: bool = False


@dataclass
class Config:
    source: SourceConfig
    dest: DestConfig
    sync: SyncConfig


def require_opt(opts: dict, key: str, where: str):
    """Fetch a required adapter option, failing with a friendly ConfigError."""
    if key not in opts or opts[key] in (None, ""):
        raise ConfigError(f"missing '{key}' in [{where}] section of the config")
    return opts[key]


def _validate(cfg: Config) -> None:
    if cfg.sync.compare != "key_size":
        raise ConfigError(
            f"unsupported compare mode '{cfg.sync.compare}'. Only 'key_size' is "
            f"implemented (ETag and modtime are unreliable cross-cloud)."
        )

    # A true mirror diffs FULL key listings of both sides. If the two sides are
    # scoped to different prefixes, everything outside the source's prefix looks
    # like an orphan on the destination and would be DELETED. Refuse the foot-gun.
    src_prefix = cfg.source.options.get("prefix", "") or ""
    dst_prefix = cfg.dest.options.get("prefix", "") or ""
    if cfg.sync.delete and src_prefix != dst_prefix:
        raise ConfigError(
            f"source prefix ({src_prefix!r}) and dest prefix ({dst_prefix!r}) "
            f"differ while delete=true. A mismatched scope would classify "
            f"everything outside the source prefix as orphans and delete it "
            f"from the destination. Use identical prefixes, or delete=false."
        )

    # Mirroring a bucket onto itself would diff a listing against itself and,
    # worse, interleave reads and writes on the same keys. Refuse.
    if cfg.source.type == "s3" and cfg.dest.type == "s3":
        same_endpoint = (cfg.source.options.get("endpoint") ==
                         cfg.dest.options.get("endpoint"))
        same_bucket = (cfg.source.options.get("bucket") ==
                       cfg.dest.options.get("bucket"))
        if same_endpoint and same_bucket:
            raise ConfigError(
                "source and destination are the same bucket on the same "
                "endpoint. Refusing to mirror a bucket onto itself."
            )
    if cfg.source.type == "local_dir" and cfg.dest.type == "local_dir":
        if os.path.realpath(str(cfg.source.options.get("path", ""))) == \
           os.path.realpath(str(cfg.dest.options.get("path", "-"))):
            raise ConfigError("source and destination are the same directory.")


def load_config(path: str) -> Config:
    with open(path, "rb") as f:
        raw = tomllib.load(f)

    src = raw.get("source", {})
    dst = raw.get("dest", {})
    syn = raw.get("sync", {})

    if "type" not in src:
        raise ConfigError("missing 'type' in [source]")
    if "type" not in dst:
        raise ConfigError("missing 'type' in [dest]")

    source = SourceConfig(type=str(src["type"]),
                          options={k: v for k, v in src.items() if k != "type"})
    dest = DestConfig(type=str(dst["type"]),
                      options={k: v for k, v in dst.items() if k != "type"})
    sync = SyncConfig(
        compare=str(syn.get("compare", "key_size")),
        delete=bool(syn.get("delete", True)),
        max_delete=str(syn.get("max_delete", "1%")),
        threads=int(syn.get("threads", 16)),
        dry_run=bool(syn.get("dry_run", True)),
        state_dir=str(syn.get("state_dir", ".state")),
        rehydrate=bool(syn.get("rehydrate", False)),
    )

    cfg = Config(source=source, dest=dest, sync=sync)
    _validate(cfg)
    return cfg


def resolve_secret(env_name: str, label: str) -> str:
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
