"""Unit tests for the sorted-merge diff. No network, no credentials."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bucketsync.diff import diff
from bucketsync.models import COPY, DELETE, ObjectInfo


def actions(src, dst):
    return [(a.op, a.key) for a in diff(iter(src), iter(dst))]


def test_new_object_is_copied():
    src = [ObjectInfo("a", 1), ObjectInfo("b", 2)]
    dst = [ObjectInfo("a", 1)]
    assert actions(src, dst) == [(COPY, "b")]


def test_orphan_is_deleted():
    src = [ObjectInfo("a", 1)]
    dst = [ObjectInfo("a", 1), ObjectInfo("z", 9)]
    assert actions(src, dst) == [(DELETE, "z")]


def test_size_change_is_recopied():
    src = [ObjectInfo("a", 5)]
    dst = [ObjectInfo("a", 1)]
    assert actions(src, dst) == [(COPY, "a")]


def test_identical_is_noop():
    src = [ObjectInfo("a", 1), ObjectInfo("b", 2)]
    dst = [ObjectInfo("a", 1), ObjectInfo("b", 2)]
    assert actions(src, dst) == []


def test_interleaved():
    src = [ObjectInfo("a", 1), ObjectInfo("c", 1), ObjectInfo("e", 1)]
    dst = [ObjectInfo("b", 1), ObjectInfo("c", 1), ObjectInfo("d", 1)]
    assert actions(src, dst) == [
        (COPY, "a"), (DELETE, "b"), (DELETE, "d"), (COPY, "e"),
    ]


def test_empty_dest_copies_all():
    src = [ObjectInfo("a", 1), ObjectInfo("b", 2)]
    assert actions(src, []) == [(COPY, "a"), (COPY, "b")]


def test_empty_source_deletes_all():
    dst = [ObjectInfo("a", 1), ObjectInfo("b", 2)]
    assert actions([], dst) == [(DELETE, "a"), (DELETE, "b")]


def test_unsorted_source_raises():
    src = [ObjectInfo("b", 1), ObjectInfo("a", 1)]
    try:
        actions(src, [])
        assert False, "expected ValueError on unsorted input"
    except ValueError:
        pass


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} tests passed")
