"""End-to-end engine test using local-dir adapters. No network, no credentials.

Exercises the real engine path: diff -> plan -> dry-run, then apply (copy +
delete), idempotency, changed-object recopy, hostile key characters, the
delete-cap abort, and the prefix-mismatch config guard.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bucketsync.adapters.local_dir import LocalDirDest, LocalDirSource
from bucketsync.config import (Config, ConfigError, DestConfig, SourceConfig,
                               SyncConfig, load_config)
from bucketsync.engine import DeleteCapExceeded, run_sync


def _write(root, rel, data):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def _keys(root):
    out = []
    for dp, _d, files in os.walk(root):
        for n in files:
            out.append(os.path.relpath(os.path.join(dp, n), root).replace(os.sep, "/"))
    return sorted(out)


def _cfg(src_dir, dst_dir, state_dir, max_delete="50%", delete=True):
    return Config(
        source=SourceConfig("local_dir", {"path": src_dir}),
        dest=DestConfig("local_dir", {"path": dst_dir}),
        sync=SyncConfig(delete=delete, max_delete=max_delete, threads=4,
                        dry_run=True, state_dir=state_dir),
    )


def main():
    with tempfile.TemporaryDirectory() as tmp:
        src_dir = os.path.join(tmp, "src")
        dst_dir = os.path.join(tmp, "dst")
        state = os.path.join(tmp, "state")

        # source: a, b (new), c (changed), hostile-key file. dest: a, c (old), orphan z.
        _write(src_dir, "patients/a.jpg", b"AAA")
        _write(src_dir, "patients/b.jpg", b"BBBB")
        _write(src_dir, "patients/c.jpg", b"CHANGED-LONGER")
        _write(src_dir, "weird\tname\nfile.bin", b"HOSTILE")   # tab + newline in key
        _write(dst_dir, "patients/a.jpg", b"AAA")
        _write(dst_dir, "patients/c.jpg", b"OLD")
        _write(dst_dir, "patients/z-orphan.jpg", b"ZZZ")

        cfg = _cfg(src_dir, dst_dir, state)

        # 1. dry-run changes nothing
        res = run_sync(cfg, LocalDirSource(cfg.source.options),
                       LocalDirDest(cfg.dest.options), apply=False)
        assert not res.applied
        assert res.plan.copy_count == 3, res.plan.copy_count   # b, c, hostile
        assert res.plan.delete_count == 1, res.plan.delete_count
        assert _keys(dst_dir) == ["patients/a.jpg", "patients/c.jpg",
                                  "patients/z-orphan.jpg"], "dry-run wrote data!"
        print("ok  dry-run: plan correct, nothing written")

        # 2. apply makes dest a true mirror, incl. the hostile key
        res = run_sync(cfg, LocalDirSource(cfg.source.options),
                       LocalDirDest(cfg.dest.options), apply=True)
        assert res.applied and res.failed == 0, (res.copy_failed, res.delete_failed)
        assert _keys(dst_dir) == _keys(src_dir), (_keys(dst_dir), _keys(src_dir))
        with open(os.path.join(dst_dir, "patients/c.jpg"), "rb") as f:
            assert f.read() == b"CHANGED-LONGER"
        with open(os.path.join(dst_dir, "weird\tname\nfile.bin"), "rb") as f:
            assert f.read() == b"HOSTILE"
        assert not os.path.exists(os.path.join(dst_dir, "patients/z-orphan.jpg"))
        print("ok  apply: destination mirrors source (copy + delete + hostile keys)")

        # 3. idempotent: a second run finds nothing to do
        res2 = run_sync(cfg, LocalDirSource(cfg.source.options),
                        LocalDirDest(cfg.dest.options), apply=True)
        assert res2.plan.copy_count == 0 and res2.plan.delete_count == 0
        print("ok  idempotent: second apply is a no-op")

        # 3b. an object that changes after a completed run is re-copied next run
        _write(src_dir, "patients/c.jpg", b"CHANGED-AGAIN-EVEN-LONGER")
        run_sync(cfg, LocalDirSource(cfg.source.options),
                 LocalDirDest(cfg.dest.options), apply=True)
        with open(os.path.join(dst_dir, "patients/c.jpg"), "rb") as f:
            assert f.read() == b"CHANGED-AGAIN-EVEN-LONGER"
        print("ok  changed object re-copied across runs")

        # 4. delete cap aborts a dangerous run
        empty_src = os.path.join(tmp, "empty")
        os.makedirs(empty_src)
        cfg_cap = _cfg(empty_src, dst_dir, os.path.join(tmp, "state2"),
                       max_delete="10%")
        try:
            run_sync(cfg_cap, LocalDirSource(cfg_cap.source.options),
                     LocalDirDest(cfg_cap.dest.options), apply=True)
            assert False, "expected DeleteCapExceeded"
        except DeleteCapExceeded:
            assert _keys(dst_dir) == _keys(src_dir), "cap abort still deleted!"
            print("ok  delete cap: aborted, destination untouched")

        # 5. prefix mismatch with delete=true is refused at config load
        import textwrap
        cfg_path = os.path.join(tmp, "bad.toml")
        with open(cfg_path, "w") as f:
            f.write(textwrap.dedent("""\
                [source]
                type = "local_dir"
                path = "/tmp/a"
                prefix = "patients/"
                [dest]
                type = "local_dir"
                path = "/tmp/b"
                [sync]
                delete = true
            """))
        try:
            load_config(cfg_path)
            assert False, "expected ConfigError for prefix mismatch"
        except ConfigError as e:
            assert "prefix" in str(e)
            print("ok  config guard: mismatched prefixes with delete=true refused")

    print("\nall engine tests passed")


if __name__ == "__main__":
    main()
