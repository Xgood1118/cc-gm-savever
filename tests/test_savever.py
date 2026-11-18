# -*- coding: utf-8 -*-
"""savever 存档迁移器测试。

覆盖：迁移链 / 幂等 / 默认值 / 未知字段 / 校验 / 确定性输出 /
边界（未来版本、损坏、空存档）/ 补丁合并 / 原子迁移 / 前向引用降级 / CLI。
"""

import ast
import hashlib
import inspect
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import savever  # noqa: E402
from savever import core  # noqa: E402

DATA_V1 = ROOT / "data" / "save_v1.json"


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------
def _clone(value):
    return json.loads(json.dumps(value))


def _dump(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _write(tmp_path, name, payload):
    path = tmp_path / name
    if isinstance(payload, bytes):
        path.write_bytes(payload)
    else:
        path.write_text(payload, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 保留的 API 名字与签名
# ---------------------------------------------------------------------------
def test_public_api_present():
    for name in ("Schema", "load_save", "migrate", "validate"):
        assert hasattr(savever, name), name


def test_public_signatures_unchanged():
    assert list(inspect.signature(savever.load_save).parameters) == ["path"]
    assert list(inspect.signature(savever.migrate).parameters) == [
        "save",
        "target_version",
    ]
    assert list(inspect.signature(savever.validate).parameters) == ["save", "schema"]
    params = list(inspect.signature(savever.Schema).parameters)
    assert params[:2] == ["version", "fields"]


def test_only_stdlib_imports():
    tree = ast.parse((ROOT / "savever" / "core.py").read_text(encoding="utf-8"))
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    assert roots <= set(sys.stdlib_module_names), roots


def test_data_save_v1_is_not_modified():
    before = hashlib.sha256(DATA_V1.read_bytes()).hexdigest()
    s = savever.load_save(str(DATA_V1))
    savever.migrate(s, 3)
    savever.migrate_atomic(s, 3)
    assert hashlib.sha256(DATA_V1.read_bytes()).hexdigest() == before


# ---------------------------------------------------------------------------
# 基本迁移契约（原有要求）
# ---------------------------------------------------------------------------
def test_migrate_to_v3():
    s = savever.load_save(str(DATA_V1))
    out = savever.migrate(s, 3)
    assert out["version"] == 3


def test_migrate_to_v3_matches_expected_save():
    s = savever.load_save(str(DATA_V1))
    out = savever.migrate(s, 3)
    assert out == {
        "version": 3,
        "gold": 10,
        "legacy_flag": True,
        "hp": 100,
        "inventory": [],
        "mana": 30,
        "difficulty": "normal",
    }


def test_idempotent():
    s = savever.load_save(str(DATA_V1))
    a = savever.migrate(s, 3)
    b = savever.migrate(a, 3)
    assert a == b
    assert _dump(a) == _dump(b)


def test_idempotent_on_every_version():
    for version in (1, 2, 3):
        s = savever.migrate({"version": 1, "gold": 1}, version)
        assert savever.migrate(s, version) == s


def test_migrate_returns_copy_not_same_object():
    s = {"version": 1, "gold": 1}
    out = savever.migrate(s, 3)
    assert out is not s
    out["gold"] = 999
    assert s == {"version": 1, "gold": 1}


def test_migrate_does_not_mutate_input():
    s = {"version": 1, "gold": 5, "nested": {"a": [1, 2]}}
    snapshot = _clone(s)
    savever.migrate(s, 3)
    assert s == snapshot


def test_chain_equals_stepwise():
    s = {"version": 1, "gold": 7}
    direct = savever.migrate(s, 3)
    step_by_step = savever.migrate(savever.migrate(s, 2), 3)
    assert direct == step_by_step


def test_migrate_to_intermediate_version():
    out = savever.migrate({"version": 1, "gold": 3}, 2)
    assert out["version"] == 2
    assert out["hp"] == 100 and out["inventory"] == []
    assert "mana" not in out and "difficulty" not in out


def test_each_step_advances_exactly_one_version():
    save = {"version": 1, "gold": 3}
    for step_from in (1, 2):
        step = savever.MIGRATION_STEPS[step_from]
        result, _filled, _downgraded = step(save)
        assert result["version"] == step_from + 1
        save = result


# ---------------------------------------------------------------------------
# 默认值补齐 / 未知字段保留
# ---------------------------------------------------------------------------
EXPECTED_DEFAULTS = {
    2: {"gold": 0, "hp": 100, "inventory": []},
    3: {"gold": 0, "hp": 100, "mana": 30, "difficulty": "normal", "inventory": []},
}


def test_missing_fields_get_fixed_defaults():
    out = savever.migrate({"version": 1}, 3)
    for key, default in EXPECTED_DEFAULTS[3].items():
        assert out[key] == default, key
    assert out["legacy_flag"] is False
    assert out["version"] == 3


def test_defaults_do_not_override_existing_values():
    s = {
        "version": 1,
        "gold": 42,
        "legacy_flag": True,
        "hp": 1,
        "inventory": ["sword"],
        "mana": 2,
        "difficulty": "hard",
    }
    out = savever.migrate(s, 3)
    assert out["gold"] == 42
    assert out["hp"] == 1
    assert out["inventory"] == ["sword"]
    assert out["mana"] == 2
    assert out["difficulty"] == "hard"
    assert out["legacy_flag"] is True


def test_defaults_are_not_shared_between_calls():
    a = savever.migrate({"version": 1}, 3)
    b = savever.migrate({"version": 1}, 3)
    a["inventory"].append("x")
    assert b["inventory"] == []


def test_unknown_fields_preserved():
    s = {"version": 1, "gold": 1, "legacy_flag": True, "custom": {"k": [1, 2]}}
    out = savever.migrate(s, 3)
    assert out["legacy_flag"] is True
    assert out["custom"] == {"k": [1, 2]}


def test_report_counts_filled_and_unknown():
    s = {"version": 1, "gold": 1, "legacy_flag": True}
    result, report = savever.migrate_with_report(s, 3)
    assert report["source_version"] == 1
    assert report["target_version"] == 3
    assert report["steps"] == ["v1->v2", "v2->v3"]
    assert report["step_count"] == 2
    assert report["filled_field_names"] == ["hp", "inventory", "mana", "difficulty"]
    assert report["filled_fields"] == 4
    assert report["unknown_field_names"] == ["legacy_flag"]
    assert report["unknown_fields"] == 1
    assert report["downgraded_fields"] == []
    assert report["validation"] == "通过"
    assert result["version"] == 3


def test_report_source_defaults_are_filled():
    result, report = savever.migrate_with_report({"version": 1}, 3)
    assert report["filled_fields"] == 6
    assert set(report["filled_field_names"]) == {
        "gold",
        "legacy_flag",
        "hp",
        "inventory",
        "mana",
        "difficulty",
    }


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------
def test_migration_output_passes_validation():
    out = savever.migrate(savever.load_save(str(DATA_V1)), 3)
    assert savever.validate(out, savever.SCHEMAS[3]) is True


def test_validate_accepts_unknown_fields():
    schema = savever.Schema(1, {"version": int, "name": str})
    assert savever.validate({"version": 1, "name": "a", "extra": 1}, schema) is True


def test_validate_reports_field_and_reason():
    with pytest.raises(savever.ValidationError) as excinfo:
        savever.validate({"version": 3, "hp": "oops"}, savever.SCHEMAS[3])
    err = excinfo.value
    names = dict(err.errors)
    assert err.field in names
    assert err.reason == names[err.field]
    assert "类型" in names["hp"]
    assert "hp" in str(err)
    assert "mana" in names and names["mana"] == "缺少字段"
    assert "difficulty" in names


def test_validate_version_mismatch_reports_field():
    with pytest.raises(savever.ValidationError) as excinfo:
        savever.validate({"version": 2}, savever.SCHEMAS[3])
    assert "version" in dict(excinfo.value.errors)


def test_validate_non_mapping():
    with pytest.raises(savever.ValidationError) as excinfo:
        savever.validate([], savever.SCHEMAS[3])
    assert "JSON 对象" in str(excinfo.value)


def test_validate_accepts_schema_version_number():
    assert savever.validate({"version": 3, **EXPECTED_DEFAULTS[3], "legacy_flag": True}, 3) is True


def test_schema_spec_forms():
    schema = savever.Schema(
        9,
        {
            "version": 9,
            "as_type": int,
            "as_default": "hello",
            "as_dict": {"type": "list", "default": [1]},
            "as_tuple": (int, float),
            "any_field": None,
        },
    )
    save = {"version": 9, "any_field": "whatever"}
    core._fill_defaults(save, schema)
    assert save["as_type"] == 0
    assert save["as_default"] == "hello"  # 只给默认值：默认值就是声明值
    assert save["as_dict"] == [1]
    assert save["as_tuple"] == 0
    assert savever.validate(
        {"version": 9, "as_type": 1, "as_default": "x", "as_dict": [],
         "as_tuple": 1.5, "any_field": None},
        schema,
    ) is True


def test_validate_rejects_bool_for_int():
    with pytest.raises(savever.ValidationError) as excinfo:
        savever.validate({"version": 1, "gold": True, "legacy_flag": True}, savever.SCHEMAS[1])
    assert "gold" in dict(excinfo.value.errors)


# ---------------------------------------------------------------------------
# 边界：未来版本 / 损坏 / 空存档
# ---------------------------------------------------------------------------
def test_future_version_save_can_load_but_not_migrate(tmp_path):
    path = _write(tmp_path, "future.json", '{"version": 4, "gold": 1}')
    save = savever.load_save(str(path))
    assert save["version"] == 4
    with pytest.raises(savever.FutureVersionError):
        savever.migrate(save, 4)


def test_future_target_rejected():
    with pytest.raises(savever.FutureVersionError):
        savever.migrate({"version": 1}, 4)


def test_downgrade_rejected():
    with pytest.raises(savever.MigrationError):
        savever.migrate({"version": 3, "hp": 1}, 2)


def test_empty_save_object_rejected():
    with pytest.raises(savever.EmptySaveError):
        savever.migrate({}, 3)


def test_migrate_non_mapping_rejected():
    with pytest.raises(savever.SaveFormatError):
        savever.migrate([], 3)


def test_missing_version_rejected():
    with pytest.raises(savever.SaveFormatError):
        savever.migrate({"gold": 1}, 3)


def test_version_wrong_type_rejected():
    with pytest.raises(savever.SaveFormatError):
        savever.migrate({"version": "1"}, 3)
    with pytest.raises(savever.SaveFormatError):
        savever.migrate({"version": True}, 3)


def test_version_below_min_rejected():
    with pytest.raises(savever.SaveFormatError):
        savever.migrate({"version": 0}, 3)


def test_target_version_wrong_type_rejected():
    with pytest.raises(savever.SaveFormatError):
        savever.migrate({"version": 1}, "3")


def test_corrupt_source_field_fails_with_field():
    with pytest.raises(savever.ValidationError) as excinfo:
        savever.migrate({"version": 1, "gold": "lots"}, 2)
    assert excinfo.value.field == "gold"
    assert "gold" in str(excinfo.value)


def test_load_empty_file(tmp_path):
    with pytest.raises(savever.EmptySaveError):
        savever.load_save(str(_write(tmp_path, "empty.json", b"")))


def test_load_whitespace_file(tmp_path):
    with pytest.raises(savever.EmptySaveError):
        savever.load_save(str(_write(tmp_path, "blank.json", b"  \n\t")))


def test_load_empty_object(tmp_path):
    with pytest.raises(savever.EmptySaveError):
        savever.load_save(str(_write(tmp_path, "empty_obj.json", "{}")))


def test_load_corrupt_json(tmp_path):
    with pytest.raises(savever.CorruptSaveError):
        savever.load_save(str(_write(tmp_path, "bad.json", "{not json")))


def test_load_non_object_root(tmp_path):
    with pytest.raises(savever.CorruptSaveError):
        savever.load_save(str(_write(tmp_path, "list.json", "[1, 2]")))


def test_load_non_utf8(tmp_path):
    with pytest.raises(savever.CorruptSaveError):
        savever.load_save(str(_write(tmp_path, "latin1.json", b'{"version": 1, "x": "\xff"}')))


def test_load_missing_file(tmp_path):
    with pytest.raises(savever.SaveFormatError):
        savever.load_save(str(tmp_path / "nope.json"))


def test_load_missing_version(tmp_path):
    with pytest.raises(savever.SaveFormatError):
        savever.load_save(str(_write(tmp_path, "noversion.json", '{"gold": 1}')))


def test_load_accepts_utf8_bom(tmp_path):
    raw = "﻿".encode("utf-8") + b'{"version": 1, "gold": 2}'
    save = savever.load_save(str(_write(tmp_path, "bom.json", raw)))
    assert save == {"version": 1, "gold": 2}


def test_boundary_outcomes_are_deterministic():
    cases = [
        ({"version": 4, "gold": 1}, 4),
        ({"version": 1, "gold": "bad"}, 2),
        ({}, 3),
    ]
    for save, target in cases:
        results = [savever.migrate_atomic(_clone(save), target) for _ in range(2)]
        assert results[0]["ok"] is False is results[1]["ok"]
        assert results[0]["failed_step"] == results[1]["failed_step"]
        assert results[0]["reason"] == results[1]["reason"]
        assert _dump(results[0]["save"]) == _dump(results[1]["save"])


# ---------------------------------------------------------------------------
# 确定性输出
# ---------------------------------------------------------------------------
def test_two_migrations_are_byte_identical():
    s = savever.load_save(str(DATA_V1))
    first = savever.dumps_save(savever.migrate(s, 3)).encode("utf-8")
    second = savever.dumps_save(savever.migrate(_clone(s), 3)).encode("utf-8")
    assert first == second


def test_dumps_is_stable():
    save = {"version": 3, "b": 1, "a": [2, 1], "nested": {"y": 1, "x": 2}}
    assert savever.dumps_save(save) == savever.dumps_save(_clone(save))
    assert savever.dumps_save(save).encode("utf-8") == savever.dumps_save(save).encode("utf-8")


def test_write_save_is_byte_identical(tmp_path):
    s = savever.load_save(str(DATA_V1))
    out = savever.migrate(s, 3)
    p1, p2 = tmp_path / "a.json", tmp_path / "b.json"
    savever.write_save(out, str(p1))
    savever.write_save(_clone(out), str(p2))
    assert p1.read_bytes() == p2.read_bytes()
    assert json.loads(p1.read_text(encoding="utf-8")) == out


# ---------------------------------------------------------------------------
# 补丁合并
# ---------------------------------------------------------------------------
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


def test_merge_keeps_unknown_fields_from_base():
    base = {"version": 3, "hp": 10, "mystery": {"a": 1}, "listy": [1, 2]}
    r = savever.merge_patches(base, [{"hp": 20}])
    assert r["conflicts"] == []
    assert r["merged"]["mystery"] == {"a": 1}
    assert r["merged"]["listy"] == [1, 2]
    assert r["merged"]["version"] == 3


def test_merge_conflict_reports_all_paths_sorted():
    r = savever.merge_patches(
        {"a": 1, "b": 2},
        [{"a": 10, "b": 20}, {"a": 11, "b": 21}],
    )
    assert r["conflicts"] == ["a", "b"]
    assert r["merged"] == {"a": 1, "b": 2}


def test_merge_conflict_does_not_silently_override():
    base = {"hp": 10}
    r = savever.merge_patches(base, [{"hp": 12}, {"hp": 15}])
    assert r["merged"]["hp"] == 10
    assert r["merged"] == base


def test_merge_conflict_when_base_lacks_field():
    r = savever.merge_patches({"version": 2}, [{"hp": 1}, {"hp": 2}])
    assert r["conflicts"] == ["hp"]
    assert "hp" not in r["merged"]


def test_merge_nested_paths():
    r = savever.merge_patches(
        {"version": 3, "stats": {"hp": 10, "mp": 5}},
        [{"stats": {"hp": 12}}, {"stats": {"mp": 8, "xp": 1}}],
    )
    assert r["conflicts"] == []
    assert r["merged"]["stats"] == {"hp": 12, "mp": 8, "xp": 1}


def test_merge_nested_conflict_path_is_dotted():
    r = savever.merge_patches(
        {"stats": {"hp": 10}},
        [{"stats": {"hp": 12}}, {"stats": {"hp": 15}}],
    )
    assert r["conflicts"] == ["stats.hp"]
    assert r["merged"]["stats"]["hp"] == 10


def test_merge_nested_new_branch():
    r = savever.merge_patches({"version": 2}, [{"stats": {"hp": 1}}, {"stats": {"mp": 2}}])
    assert r["conflicts"] == []
    assert r["merged"]["stats"] == {"hp": 1, "mp": 2}


def test_merge_leaf_vs_object_is_conflict():
    r = savever.merge_patches({}, [{"hp": 1}, {"hp": {"max": 2}}])
    assert r["conflicts"] == ["hp"]
    assert "hp" not in r["merged"]


def test_merge_base_scalar_vs_patch_object_is_conflict():
    r = savever.merge_patches({"stats": 5}, [{"stats": {"hp": 1}}])
    assert "stats" in r["conflicts"]
    assert r["merged"]["stats"] == 5


def test_merge_bool_and_int_are_not_same_value():
    r = savever.merge_patches({"legacy_flag": False}, [{"legacy_flag": True}, {"legacy_flag": 1}])
    assert r["conflicts"] == ["legacy_flag"]
    assert r["merged"]["legacy_flag"] is False


def test_merge_int_and_float_are_not_same_value():
    r = savever.merge_patches({}, [{"x": 1}, {"x": 1.0}])
    assert r["conflicts"] == ["x"]


def test_merge_equal_lists_and_dicts_merge():
    value = {"a": [1, {"b": 2}]}
    r = savever.merge_patches({}, [{"v": value}, {"v": _clone(value)}])
    assert r["conflicts"] == []
    assert r["merged"]["v"] == value


def test_merge_different_lists_conflict():
    r = savever.merge_patches({"inv": []}, [{"inv": [1]}, {"inv": [2]}])
    assert r["conflicts"] == ["inv"]
    assert r["merged"]["inv"] == []


def test_merge_empty_patches_returns_base_copy():
    base = {"version": 2, "hp": 1}
    r = savever.merge_patches(base, [])
    assert r["conflicts"] == []
    assert r["merged"] == base
    assert r["merged"] is not base


def test_merge_does_not_mutate_inputs():
    base = {"version": 2, "hp": 10, "nested": {"a": 1}}
    patches = [{"hp": 20, "nested": {"a": 1}}, {"mp": 3}]
    base_snapshot = _clone(base)
    patches_snapshot = _clone(patches)
    r = savever.merge_patches(base, patches)
    assert base == base_snapshot
    assert patches == patches_snapshot
    r["merged"]["nested"]["a"] = 99
    assert base == base_snapshot


def test_merge_is_deterministic():
    base = {"a": 1, "b": 2, "nested": {"c": 3}}
    patches = [{"a": 9, "nested": {"c": 4}}, {"a": 8, "d": 5}]
    first = savever.dumps_save(savever.merge_patches(base, patches))
    second = savever.dumps_save(savever.merge_patches(_clone(base), _clone(patches)))
    assert first == second


def test_merge_rejects_bad_arguments():
    with pytest.raises(TypeError):
        savever.merge_patches([], [{}])
    with pytest.raises(TypeError):
        savever.merge_patches({}, 3)
    with pytest.raises(TypeError):
        savever.merge_patches({}, ["not-a-dict"])


def test_merge_result_survives_migration_validation():
    base = savever.migrate({"version": 1, "gold": 1}, 2)
    r = savever.merge_patches(base, [{"hp": 50}, {"mp": 9}])
    assert r["conflicts"] == []
    assert savever.validate(r["merged"], savever.SCHEMAS[2]) is True
    assert savever.migrate(r["merged"], 3)["mp"] == 9


# ---------------------------------------------------------------------------
# 原子迁移
# ---------------------------------------------------------------------------
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


def test_atomic_success_equals_plain_migrate():
    for save, target in (
        ({"version": 1, "gold": 1}, 3),
        ({"version": 2, "hp": 5}, 3),
        ({"version": 3, "hp": 5, "mana": 1}, 3),
        ({"version": 1, "hp": "bad"}, 3),
    ):
        r = savever.migrate_atomic(_clone(save), target)
        assert r["ok"] is True
        assert r["save"] == savever.migrate(_clone(save), target)
        assert _dump(r["save"]) == _dump(savever.migrate(_clone(save), target))


def test_atomic_failure_reports_step_and_reason():
    r = savever.migrate_atomic({"version": 1, "gold": "bad"}, 3)
    assert r["ok"] is False
    assert r["failed_step"] == "validate-source@v1"
    assert "gold" in r["reason"]
    assert r["error_type"] == "ValidationError"
    assert r["save"] == {"version": 1, "gold": "bad"}


def test_atomic_future_version_fails_at_precheck():
    r = savever.migrate_atomic({"version": 4, "gold": 1}, 4)
    assert r["ok"] is False
    assert r["failed_step"] == "precheck"
    assert "高于最高支持版本" in r["reason"]


def test_atomic_downgrade_fails_at_precheck():
    r = savever.migrate_atomic({"version": 3, "hp": 1}, 1)
    assert r["ok"] is False
    assert r["failed_step"] == "precheck"


def test_atomic_empty_save_keeps_source():
    for bad in ({}, []):
        snapshot = _clone(bad)
        r = savever.migrate_atomic(bad, 3)
        assert r["ok"] is False
        assert r["failed_step"] == "precheck"
        assert bad == snapshot


def test_atomic_does_not_mutate_source_on_success():
    src = {"version": 1, "gold": 1, "extras": {"x": [1]}}
    snapshot = _clone(src)
    r = savever.migrate_atomic(src, 3)
    assert r["ok"] is True
    assert src == snapshot


def test_atomic_failure_result_save_is_independent_copy():
    bad = {"version": 1, "gold": "bad"}
    r = savever.migrate_atomic(bad, 3)
    r["save"]["gold"] = 1
    assert bad["gold"] == "bad"


def test_atomic_failure_inside_step_reports_step(monkeypatch):
    def boom(save):
        raise RuntimeError("模拟步骤崩溃")

    monkeypatch.setitem(savever.MIGRATION_STEPS, 1, boom)
    src = {"version": 1, "gold": 1}
    snapshot = _clone(src)
    r = savever.migrate_atomic(src, 2)
    assert r["ok"] is False
    assert r["failed_step"] == "v1->v2"
    assert "模拟步骤崩溃" in r["reason"]
    assert src == snapshot


# ---------------------------------------------------------------------------
# 前向引用降级
# ---------------------------------------------------------------------------
def test_forward_reference_valid_value_kept():
    save = {"version": 1, "gold": 1, "hp": 55, "inventory": ["a"], "mana": 9}
    result, report = savever.migrate_with_report(save, 3)
    assert result["hp"] == 55
    assert result["inventory"] == ["a"]
    assert result["mana"] == 9
    assert report["downgraded_fields"] == []


def test_forward_reference_bad_type_downgraded():
    save = {"version": 1, "gold": 1, "hp": "not-int"}
    result, report = savever.migrate_with_report(save, 2)
    assert result["hp"] == 100
    assert report["downgraded_fields"] == ["hp"]


def test_forward_reference_downgraded_at_right_step():
    save = {"version": 1, "gold": 1, "mana": "bad"}
    to_v2, report2 = savever.migrate_with_report(save, 2)
    assert to_v2["mana"] == "bad"          # v2 不声明 mana，作为未知字段保留
    assert report2["downgraded_fields"] == []

    to_v3, report3 = savever.migrate_with_report(save, 3)
    assert to_v3["mana"] == 30             # v3 声明 mana，类型不符 -> 回落默认值
    assert report3["downgraded_fields"] == ["mana"]


def test_forward_reference_bool_vs_int():
    result, report = savever.migrate_with_report({"version": 1, "hp": True}, 2)
    assert result["hp"] == 100
    assert report["downgraded_fields"] == ["hp"]


def test_forward_reference_does_not_crash_or_lose_other_fields():
    save = {"version": 1, "gold": 1, "hp": None, "inventory": 5, "custom": "keep"}
    result, report = savever.migrate_with_report(save, 3)
    assert result["hp"] == 100
    assert result["inventory"] == []
    assert result["custom"] == "keep"
    assert result["gold"] == 1
    assert set(report["downgraded_fields"]) == {"hp", "inventory"}


# ---------------------------------------------------------------------------
# CLI 验收
# ---------------------------------------------------------------------------
def _run_cli(*args, cwd=ROOT):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, "-m", "savever.cli", *map(str, args)],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def test_cli_end_to_end(tmp_path):
    out = tmp_path / "save_v3.json"
    proc = _run_cli(DATA_V1, "--to", 3, "--out", out)
    assert proc.returncode == 0, proc.stderr
    expected = savever.migrate(savever.load_save(str(DATA_V1)), 3)
    assert json.loads(out.read_text(encoding="utf-8")) == expected
    assert out.read_bytes() == savever.dumps_save(expected).encode("utf-8")
    assert "迁移步数：2" in proc.stdout
    assert "补齐字段数：4" in proc.stdout
    assert "保留未知字段数：1" in proc.stdout


def test_cli_output_is_byte_identical_across_runs(tmp_path):
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    assert _run_cli(DATA_V1, "--to", 3, "--out", a).returncode == 0
    assert _run_cli(DATA_V1, "--to", 3, "--out", b).returncode == 0
    assert a.read_bytes() == b.read_bytes()


def test_cli_atomic_success(tmp_path):
    out = tmp_path / "atomic.json"
    proc = _run_cli(DATA_V1, "--to", 3, "--out", out, "--atomic")
    assert proc.returncode == 0, proc.stderr
    assert json.loads(out.read_text(encoding="utf-8"))["version"] == 3


def test_cli_failure_exit_code_and_no_output(tmp_path):
    src = _write(tmp_path, "future.json", '{"version": 4, "gold": 1}')
    out = tmp_path / "never.json"
    proc = _run_cli(src, "--to", 5, "--out", out)
    assert proc.returncode == 1
    assert "迁移失败" in proc.stderr
    assert not out.exists()


def test_cli_atomic_failure_keeps_input(tmp_path):
    src = _write(tmp_path, "corrupt_field.json", '{"version": 3, "hp": "not-int"}')
    before = src.read_bytes()
    out = tmp_path / "never.json"
    proc = _run_cli(src, "--to", 4, "--out", out, "--atomic")
    assert proc.returncode == 1
    assert "迁移失败" in proc.stderr
    assert src.read_bytes() == before
    assert not out.exists()


def test_cli_corrupt_input(tmp_path):
    src = _write(tmp_path, "bad.json", "{oops")
    proc = _run_cli(src, "--to", 3, "--out", tmp_path / "x.json")
    assert proc.returncode == 1
    assert "读取失败" in proc.stderr


def test_cli_help_works():
    proc = _run_cli("--help")
    assert proc.returncode == 0
    assert "--to" in proc.stdout and "--out" in proc.stdout
