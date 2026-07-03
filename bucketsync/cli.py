"""Command-line entry point.

Dry-run is the default. Writing requires an explicit --apply. Credentials are
never accepted as flags; they come from the environment via the config.

Exit codes (stable, for cron/systemd monitoring):
  0  success (dry-run printed, or apply completed with zero failures)
  2  configuration error
  3  delete cap exceeded — run aborted before any change
  4  apply completed but some copies/deletes failed (re-run to retry)
  5  another run already holds the lock for this state dir
  130 interrupted
"""

from __future__ import annotations

import argparse
import sys

from . import __version__
from .adapters import make_dest, make_source
from .config import ConfigError, load_config
from .engine import DeleteCapExceeded, RunLockHeld, run_sync


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bucket-delta-sync",
        description="One-way incremental mirror between cloud object stores. "
                    "The destination becomes a true mirror of the source. "
                    "The source is never written to.",
    )
    p.add_argument("-c", "--config", default="config.toml",
                   help="path to config TOML (default: %(default)s)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--apply", dest="apply", action="store_true", default=None,
                   help="actually copy and delete (default is dry-run)")
    g.add_argument("--dry-run", dest="apply", action="store_false",
                   help="force a dry-run even if the config sets dry_run=false")
    p.add_argument("--threads", type=int, default=None,
                   help="override worker thread count")
    p.add_argument("--state-dir", default=None,
                   help="override directory for plan files and the run lock")
    p.add_argument("--version", action="version",
                   version=f"%(prog)s {__version__}")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
    except (OSError, ConfigError) as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2

    if args.threads is not None:
        config.sync.threads = args.threads
    if args.state_dir is not None:
        config.sync.state_dir = args.state_dir

    print(f"bucket-delta-sync {__version__}")
    print(f"  source: {config.source.type} (read-only)  ->  dest: {config.dest.type}")

    source = dest = None
    try:
        source = make_source(config.source)
        dest = make_dest(config.dest)
        result = run_sync(config, source, dest, apply=args.apply)
        if result.applied and result.failed:
            return 4
    except DeleteCapExceeded as e:
        print(f"\nABORTED: {e}", file=sys.stderr)
        return 3
    except RunLockHeld as e:
        print(f"\nSKIPPED: {e}", file=sys.stderr)
        return 5
    except (ConfigError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted. Re-run to resume; the diff picks up where it left off.",
              file=sys.stderr)
        return 130
    finally:
        for a in (source, dest):
            if a is not None:
                try:
                    a.close()
                except Exception:
                    pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
