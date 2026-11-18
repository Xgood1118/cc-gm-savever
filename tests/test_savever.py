# -*- coding: utf-8 -*-
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import savever as mod  # noqa: E402


def test_migrate_to_v3():
    s = savever.load_save("data/save_v1.json")
    out = savever.migrate(s, 3)
    assert out["version"] == 3


def test_idempotent():
    s = savever.load_save("data/save_v1.json")
    a = savever.migrate(s, 3)
    b = savever.migrate(a, 3)
    assert a == b


# ===== v2 契约测试：初始全部失败，实现后必须通过 =====
import savever  # noqa: E402


def test_merge_same_value():
    r = savever.merge_patches({"version": 2, "hp": 10, "mp": 5}, [{"hp": 12}, {"hp": 12, "mp": 7}])
    assert r["conflicts"] == []
    assert r["merged"]["hp"] == 12 and r["merged"]["mp"] == 7


def test_merge_conflict_path():
    r = savever.merge_patches({"hp": 10}, [{"hp": 12}, {"hp": 15}])
    assert r["conflicts"] and "hp" in r["conflicts"][0]
    assert r["merged"]["hp"] == 10


def test_merge_keeps_unmentioned():
    r = savever.merge_patches({"version": 2, "hp": 10, "legacy_flag": True}, [{"mp": 3}])
    assert r["merged"]["hp"] == 10 and r["merged"]["legacy_flag"] is True


def test_atomic_failure_keeps_source():
    bad = {"version": 3, "hp": "not-int"}
    src = json.dumps(bad, sort_keys=True)
    r = savever.migrate_atomic(bad, 4)
    assert r["ok"] is False
    assert json.dumps(bad, sort_keys=True) == src
    assert r.get("reason")


def test_atomic_success_version():
    r = savever.migrate_atomic({"version": 1, "hp": 10}, 3)
    assert r["ok"] is True
    assert r["save"]["version"] == 3
