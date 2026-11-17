# -*- coding: utf-8 -*-
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import savever as mod  # noqa: E402
import savever.cli as cli_mod  # noqa: E402

savever = mod


def read_text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def read_hex(path):
    with open(path, encoding="utf-8") as f:
        return f.read().strip()


def read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def write_json(path, obj, raw=None):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(raw if raw is not None else json.dumps(obj))
    return str(path)


# ---------------------------------------------------------------------------
# 基础
# ---------------------------------------------------------------------------
def test_migrate_to_v3():
    s = savever.load_save("data/save_v1.json")
    out = savever.migrate(s, 3)
    assert out["version"] == 3


def test_idempotent():
    s = savever.load_save("data/save_v1.json")
    a = savever.migrate(s, 3)
    b = savever.migrate(a, 3)
    assert a == b


def test_original_save_not_mutated():
    s = {"version": 1, "gold": 10, "legacy_flag": True}
    before = json.loads(json.dumps(s))
    savever.migrate(s, 3)
    assert s == before


# ---------------------------------------------------------------------------
# 多版本链式迁移
# ---------------------------------------------------------------------------
def test_chain_equals_step_by_step():
    s = savever.load_save("data/save_v1.json")
    direct = savever.migrate(s, 3)
    step1 = savever.migrate(s, 2)
    step2 = savever.migrate(step1, 3)
    assert direct == step2
    assert direct == savever.migrate(savever.migrate(s, 1), 3)


def test_chain_report_steps_and_fields():
    s = savever.load_save("data/save_v1.json")
    out, report = savever.migrate_with_report(s, 3)
    assert report["source_version"] == 1
    assert report["target_version"] == 3
    assert report["steps"] == 2
    assert report["filled_fields"] == 4  # hp, inventory, mana, difficulty
    assert report["unknown_fields"] == 1  # legacy_flag
    assert report["unknown_field_names"] == ["legacy_flag"]
    assert report["validation"] == "通过"
    assert out["version"] == 3


def test_migrate_v1_to_v2_values():
    s = savever.load_save("data/save_v1.json")
    out = savever.migrate(s, 2)
    assert out == {
        "version": 2,
        "gold": 10,
        "legacy_flag": True,
        "hp": 100,
        "inventory": [],
    }


def test_migrate_v3_to_v3_is_identity():
    s = savever.load_save("data/save_v1.json")
    v3 = savever.migrate(s, 3)
    out, report = savever.migrate_with_report(v3, 3)
    assert out == v3
    assert report["steps"] == 0
    assert report["filled_fields"] == 0
    assert report["unknown_fields"] == 1


# ---------------------------------------------------------------------------
# 默认值补齐
# ---------------------------------------------------------------------------
def test_defaults_filled_for_minimal_save():
    out = savever.migrate({"version": 1}, 3)
    assert out == {
        "version": 3,
        "gold": 0,
        "legacy_flag": False,  # v1 自己的默认值，随后作为未知字段保留
        "hp": 100,
        "mana": 30,
        "difficulty": "normal",
        "inventory": [],
    }


def test_defaults_do_not_override_existing_values():
    s = {"version": 1, "gold": 999, "hp": 7, "difficulty": "hard"}
    out = savever.migrate(s, 3)
    assert out["gold"] == 999
    assert out["hp"] == 7
    assert out["difficulty"] == "hard"


def test_defaults_are_not_shared_between_results():
    a = savever.migrate({"version": 1}, 3)
    b = savever.migrate({"version": 1}, 3)
    a["inventory"].append("sword")
    assert b["inventory"] == []


# ---------------------------------------------------------------------------
# 未知字段保留
# ---------------------------------------------------------------------------
def test_unknown_fields_preserved():
    s = {"version": 1, "gold": 1, "legacy_flag": True, "custom": {"x": [1, 2]}}
    out = savever.migrate(s, 3)
    assert out["legacy_flag"] is True
    assert out["custom"] == {"x": [1, 2]}


def test_unknown_field_survives_each_step():
    s = {"version": 1, "gold": 1, "note": "hello"}
    v2 = savever.migrate(s, 2)
    v3 = savever.migrate(v2, 3)
    assert v2["note"] == "hello"
    assert v3["note"] == "hello"


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------
def test_validate_ok():
    s = savever.migrate({"version": 1}, 3)
    assert savever.validate(s, savever.SCHEMAS[3]) is True


def test_validate_reports_field_and_reason_missing():
    s = {"version": 3, "gold": 0, "mana": 30, "difficulty": "normal", "inventory": []}
    with pytest.raises(savever.ValidationError) as ei:
        savever.validate(s, savever.SCHEMAS[3])
    exc = ei.value
    assert exc.field == "hp"
    assert "hp" in str(exc)
    assert "缺少字段" in str(exc)
    assert ("hp", "缺少字段") in exc.errors


def test_validate_reports_field_and_reason_type():
    s = savever.migrate({"version": 1}, 3)
    s["gold"] = "10"
    with pytest.raises(savever.ValidationError) as ei:
        savever.validate(s, savever.SCHEMAS[3])
    exc = ei.value
    assert exc.field == "gold"
    assert "gold" in str(exc)
    assert "类型错误" in str(exc)


def test_validate_bool_is_not_int():
    s = savever.migrate({"version": 1}, 3)
    s["gold"] = True
    with pytest.raises(savever.ValidationError) as ei:
        savever.validate(s, savever.SCHEMAS[3])
    assert ei.value.field == "gold"


def test_validate_wrong_version():
    s = savever.migrate({"version": 1}, 2)
    with pytest.raises(savever.ValidationError) as ei:
        savever.validate(s, savever.SCHEMAS[3])
    assert ei.value.field == "version"
    assert "版本不匹配" in str(ei.value)


# ---------------------------------------------------------------------------
# 边界：未来版本 / 损坏 / 空
# ---------------------------------------------------------------------------
def test_future_version_loads_but_migrate_rejects(tmp_path):
    path = write_json(tmp_path / "future.json", {"version": 4, "gold": 1})
    s = savever.load_save(str(path))
    assert s["version"] == 4
    with pytest.raises(savever.FutureVersionError):
        savever.migrate(s, 4)
    with pytest.raises(savever.FutureVersionError):
        savever.migrate(s, 3)


def test_future_target_rejected():
    with pytest.raises(savever.FutureVersionError):
        savever.migrate({"version": 1}, 99)


def test_downgrade_rejected():
    s = savever.migrate({"version": 1}, 3)
    with pytest.raises(savever.MigrationError):
        savever.migrate(s, 1)


def test_broken_json(tmp_path):
    path = write_json(tmp_path / "broken.json", None, raw='{"version": 1,')
    with pytest.raises(savever.CorruptSaveError) as ei:
        savever.load_save(str(path))
    assert "损坏" in str(ei.value)


def test_broken_non_object_root(tmp_path):
    path = write_json(tmp_path / "list.json", [1, 2, 3])
    with pytest.raises(savever.CorruptSaveError):
        savever.load_save(str(path))


def test_empty_file(tmp_path):
    path = write_json(tmp_path / "empty.json", None, raw="")
    with pytest.raises(savever.EmptySaveError):
        savever.load_save(str(path))


def test_empty_object(tmp_path):
    path = write_json(tmp_path / "empty_obj.json", {})
    with pytest.raises(savever.EmptySaveError):
        savever.load_save(str(path))


def test_empty_object_migration_rejected():
    with pytest.raises(savever.EmptySaveError):
        savever.migrate({}, 3)


def test_missing_version_field(tmp_path):
    path = write_json(tmp_path / "no_version.json", {"gold": 1})
    with pytest.raises(savever.SaveFormatError) as ei:
        savever.load_save(str(path))
    assert "version" in str(ei.value)


def test_version_as_string_rejected(tmp_path):
    path = write_json(tmp_path / "str_version.json", {"version": "1"})
    with pytest.raises(savever.SaveFormatError):
        savever.load_save(str(path))


def test_missing_file():
    with pytest.raises(savever.SaveFormatError):
        savever.load_save("data/does_not_exist.json")


# ---------------------------------------------------------------------------
# 确定性输出
# ---------------------------------------------------------------------------
def test_bytes_stable_across_two_runs(tmp_path):
    s = savever.load_save("data/save_v1.json")
    p1 = tmp_path / "a.json"
    p2 = tmp_path / "b.json"
    savever.write_save(savever.migrate(s, 3), str(p1))
    savever.write_save(savever.migrate(s, 3), str(p2))
    assert read_bytes(str(p1)) == read_bytes(str(p2))


def test_bytes_stable_for_dict_ordering(tmp_path):
    a = {"version": 1, "gold": 10, "legacy_flag": True}
    b = {"legacy_flag": True, "gold": 10, "version": 1}
    p1 = tmp_path / "a.json"
    p2 = tmp_path / "b.json"
    savever.write_save(savever.migrate(a, 3), str(p1))
    savever.write_save(savever.migrate(b, 3), str(p2))
    assert read_bytes(str(p1)) == read_bytes(str(p2))


def test_dumps_is_utf8_and_lf():
    text = savever.dumps_save({"version": 3, "note": "中文"})
    assert "中文" in text
    assert "\r" not in text
    assert text.endswith("\n")


def test_round_trip(tmp_path):
    s = savever.load_save("data/save_v1.json")
    out = savever.migrate(s, 3)
    path = tmp_path / "v3.json"
    savever.write_save(out, str(path))
    assert savever.load_save(str(path)) == out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def test_cli_end_to_end(tmp_path, capsys):
    out = tmp_path / "save_v3.json"
    code = cli_mod.main(["data/save_v1.json", "--to", "3", "--out", str(out)])
    assert code == 0
    printed = capsys.readouterr().out
    assert "迁移步数：2" in printed
    assert "补齐字段数：4" in printed
    assert "保留未知字段数：1" in printed
    assert "校验结果：通过" in printed
    data = json.loads(read_text(str(out)))
    assert data["version"] == 3
    assert data["legacy_flag"] is True


def test_cli_failure_exit_code(tmp_path, capsys):
    path = write_json(tmp_path / "future.json", {"version": 4})
    code = cli_mod.main([str(path), "--to", "4", "--out", str(tmp_path / "o.json")])
    assert code == 1
    assert "迁移失败" in capsys.readouterr().err
