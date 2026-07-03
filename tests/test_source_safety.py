"""Source-safety tests: the engine must never mutate the source.

Runs a full mirror (copies AND deletes) against a tripwire source whose
mutating methods blow up if ever invoked, and checks the read-only proxy blocks
destination-style calls on the source structurally.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bucketsync.adapters.local_dir import LocalDirDest, LocalDirSource
from bucketsync.config import Config, DestConfig, SourceConfig, SyncConfig
from bucketsync.engine import _SourceReadOnly, run_sync


class TripwireSource(LocalDirSource):
    """A source that records any attempt to mutate it.

    It grows decoy mutating methods with the same names a destination adapter
    has. If the engine ever calls one, the test fails.
    """

    def __init__(self, opts):
        super().__init__(opts)
        self.mutations = []

    def put_object(self, *a, **k):
        self.mutations.append(("put_object", a))
        raise AssertionError("engine wrote to the SOURCE")

    def delete_keys(self, *a, **k):
        self.mutations.append(("delete_keys", a))
        raise AssertionError("engine deleted from the SOURCE")

    def start_restore(self, key):
        self.mutations.append(("start_restore", key))
        raise AssertionError("engine rehydrated on the SOURCE without opt-in")


def _write(root, rel, data):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def main():
    with tempfile.TemporaryDirectory() as tmp:
        src_dir = os.path.join(tmp, "src")
        dst_dir = os.path.join(tmp, "dst")
        _write(src_dir, "patients/a.jpg", b"AAA")
        _write(src_dir, "patients/b.jpg", b"BBBB")
        _write(dst_dir, "patients/a.jpg", b"OLD-SIZE-DIFFERS")   # recopy
        _write(dst_dir, "orphan.bin", b"ZZZ")                     # delete

        cfg = Config(
            source=SourceConfig("local_dir", {"path": src_dir}),
            dest=DestConfig("local_dir", {"path": dst_dir}),
            sync=SyncConfig(delete=True, max_delete="90%", threads=4,
                            dry_run=False, state_dir=os.path.join(tmp, "state"),
                            rehydrate=False),
        )
        tripwire = TripwireSource(cfg.source.options)

        # Full mirror with copies AND deletes: the tripwire must stay silent.
        res = run_sync(cfg, tripwire, LocalDirDest(cfg.dest.options), apply=True)
        assert res.failed == 0, (res.copy_failed, res.delete_failed)
        assert tripwire.mutations == [], f"SOURCE WAS MUTATED: {tripwire.mutations}"
        # source files still intact
        assert sorted(os.listdir(os.path.join(src_dir, "patients"))) == \
            ["a.jpg", "b.jpg"]
        print("ok  full mirror ran; zero mutating calls reached the source")

        # The read-only proxy blocks destination-style calls structurally.
        proxy = _SourceReadOnly(tripwire)
        for name in ("put_object", "delete_keys"):
            try:
                getattr(proxy, name)
                assert False, f"proxy exposed {name}"
            except AttributeError:
                pass
        print("ok  read-only proxy: put_object/delete_keys not expressible")

        # Cold-tier objects are skipped and reported when rehydrate=false,
        # with no restore call reaching the source.
        from bucketsync.engine import _copy_phase, Plan
        import json
        plan_file = os.path.join(tmp, "cold.plan")
        with open(plan_file, "w") as f:
            f.write(json.dumps(["frozen.jpg", 10, "Archive", None]) + "\n")
        plan = Plan(plan_file, plan_file, 1, 10, 0, 0, 1)
        prog = _copy_phase(_SourceReadOnly(tripwire), LocalDirDest(cfg.dest.options),
                           plan, 2, rehydrate=False)
        assert prog.failed == 1 and prog.ok == 0
        assert tripwire.mutations == [], "rehydrate=false still touched the source"
        print("ok  archive blob skipped+reported, source untouched (rehydrate=false)")

    print("\nall source-safety tests passed")


if __name__ == "__main__":
    main()
