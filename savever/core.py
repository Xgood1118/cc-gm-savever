# -*- coding: utf-8 -*-
"""存档版本迁移。

设计要点：

* 每个版本只有一个单步迁移函数（v1 -> v2、v2 -> v3），``migrate`` 负责把
  这些单步按顺序串成迁移链，因此可以一次跨多个版本。
* 每一步都只"补默认值 / 改格式"，不会删除任何已有字段。schema 未声明的
  字段（未知字段）原样保留。
* 迁移结果在返回前一定会通过 ``validate`` 校验。
* 所有默认值都是固定值，序列化时按键排序，保证同样的输入得到同样的字节。
"""

from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass

__all__ = [
    "Schema",
    "SCHEMAS",
    "MIN_VERSION",
    "LATEST_VERSION",
    "SaveError",
    "SaveFormatError",
    "EmptySaveError",
    "CorruptSaveError",
    "FutureVersionError",
    "MigrationError",
    "ValidationError",
    "load_save",
    "migrate",
    "migrate_with_report",
    "validate",
    "dumps_save",
    "write_save",
]


MIN_VERSION = 1
LATEST_VERSION = 3


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------
class SaveError(ValueError):
    """存档相关错误的基类（继承 ValueError，方便调用方按 ValueError 捕获）。"""


class SaveFormatError(SaveError):
    """存档结构不合法。"""


class EmptySaveError(SaveFormatError):
    """空存档。"""


class CorruptSaveError(SaveFormatError):
    """存档内容损坏（无法解码或不是合法 JSON）。"""


class FutureVersionError(SaveError):
    """存档或目标版本超出当前支持范围。"""


class MigrationError(SaveError):
    """迁移方向不合法（例如要求降级）。"""


class ValidationError(SaveError):
    """校验失败，``errors`` 中逐项给出字段名与原因。"""

    def __init__(self, errors, message=None):
        self.errors = list(errors)
        self.field = self.errors[0][0] if self.errors else None
        self.reason = self.errors[0][1] if self.errors else None
        if message is None:
            message = "校验失败：" + "；".join(
                '字段 "%s" %s' % (name, reason) for name, reason in self.errors
            )
        super().__init__(message)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
@dataclass
class Schema:
    """版本 schema。

    ``fields`` 是 ``{字段名: 规格}``。规格可以是：

    * ``{"type": int, "default": 0}``（本模块内部使用的完整形式）
    * ``int``（只声明类型）
    * ``0``（只给出默认值，类型由默认值推断）
    """

    version: int
    fields: dict


SCHEMAS = {
    1: Schema(
        1,
        {
            "version": {"type": int, "default": 1},
            "gold": {"type": int, "default": 0},
            "legacy_flag": {"type": bool, "default": False},
        },
    ),
    2: Schema(
        2,
        {
            "version": {"type": int, "default": 2},
            "gold": {"type": int, "default": 0},
            "hp": {"type": int, "default": 100},
            "inventory": {"type": list, "default": []},
        },
    ),
    3: Schema(
        3,
        {
            "version": {"type": int, "default": 3},
            "gold": {"type": int, "default": 0},
            "hp": {"type": int, "default": 100},
            "mana": {"type": int, "default": 30},
            "difficulty": {"type": str, "default": "normal"},
            "inventory": {"type": list, "default": []},
        },
    ),
}


# ---------------------------------------------------------------------------
# 规格 / 类型工具
# ---------------------------------------------------------------------------
def _is_int(value):
    """严格判断 int（bool 是 int 的子类，这里要排除）。"""
    return isinstance(value, int) and not isinstance(value, bool)


def _type_name(expected):
    if expected is None:
        return "任意"
    if isinstance(expected, tuple):
        return " 或 ".join(_type_name(item) for item in expected)
    return getattr(expected, "__name__", str(expected))


def _type_ok(value, expected):
    if expected is None:
        return True
    if isinstance(expected, tuple):
        return any(_type_ok(value, item) for item in expected)
    if expected is int:
        return _is_int(value)
    if expected is float:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, expected)


def _spec_type(spec):
    if isinstance(spec, dict):
        if "type" in spec:
            return spec["type"]
        if "default" in spec:
            return type(spec["default"])
        return None
    if isinstance(spec, (type, tuple)):
        return spec
    return type(spec)


def _spec_default(spec):
    if isinstance(spec, dict):
        if "default" in spec:
            return spec["default"]
        factory = spec.get("default_factory")
        if callable(factory):
            return factory()
        return None
    if isinstance(spec, tuple) and len(spec) == 2:
        return spec[1]
    if isinstance(spec, type):
        return {int: 0, float: 0.0, str: "", bool: False, list: list, dict: dict}.get(
            spec
        )
    return spec


def _fill_defaults(save, schema):
    """给缺失字段补上固定默认值，返回补齐的字段数。已有字段（包括未知字段）不动。"""
    filled = 0
    for name, spec in schema.fields.items():
        if name not in save:
            save[name] = copy.deepcopy(_spec_default(spec))
            filled += 1
    return filled


def _require_version(save, where="存档"):
    if not isinstance(save, dict):
        raise SaveFormatError(
            "%s必须是 JSON 对象，实际为 %s" % (where, type(save).__name__)
        )
    if not save:
        raise EmptySaveError("%s为空：JSON 对象不含任何字段" % where)
    if "version" not in save:
        raise SaveFormatError('%s缺少 "version" 字段' % where)
    version = save["version"]
    if not _is_int(version):
        raise SaveFormatError(
            '字段 "version" 类型错误：期望 int，实际 %s' % type(version).__name__
        )
    if version < MIN_VERSION:
        raise SaveFormatError("不支持的存档版本 %r：版本号必须 >= %d" % (version, MIN_VERSION))
    return version


# ---------------------------------------------------------------------------
# 单步迁移：每个函数只负责 version -> version + 1
# ---------------------------------------------------------------------------
def _migrate_1_to_2(save):
    result = copy.deepcopy(save)
    result["version"] = 2
    filled = _fill_defaults(result, SCHEMAS[2])
    return result, filled


def _migrate_2_to_3(save):
    result = copy.deepcopy(save)
    result["version"] = 3
    filled = _fill_defaults(result, SCHEMAS[3])
    return result, filled


_MIGRATION_STEPS = {
    1: _migrate_1_to_2,
    2: _migrate_2_to_3,
}


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------
def validate(save, schema):
    """按 ``schema`` 校验存档。

    成功返回 ``True``；失败抛出 :class:`ValidationError`，消息与 ``errors``
    属性中都会包含字段名和具体原因。schema 未声明的字段视为未知字段，允许
    存在（迁移器会保留它们）。
    """
    if not isinstance(schema, Schema):
        raise SaveFormatError("schema 必须是 Schema 实例，实际为 %s" % type(schema).__name__)
    if not isinstance(save, dict):
        raise ValidationError([("<存档>", "必须是 JSON 对象，实际为 %s" % type(save).__name__)])

    errors = []
    declared = dict(schema.fields or {})

    version_spec = declared.get("version", {"type": int})
    if "version" not in save:
        errors.append(("version", "缺少字段"))
    else:
        version = save["version"]
        if not _type_ok(version, _spec_type(version_spec)):
            errors.append(
                (
                    "version",
                    "类型错误：期望 %s，实际 %s"
                    % (_type_name(_spec_type(version_spec)), type(version).__name__),
                )
            )
        elif version != schema.version:
            errors.append(
                ("version", "版本不匹配：期望 %d，实际 %r" % (schema.version, version))
            )

    for name, spec in declared.items():
        if name == "version":
            continue
        if name not in save:
            errors.append((name, "缺少字段"))
            continue
        expected = _spec_type(spec)
        if not _type_ok(save[name], expected):
            errors.append(
                (
                    name,
                    "类型错误：期望 %s，实际 %s"
                    % (_type_name(expected), type(save[name]).__name__),
                )
            )

    if errors:
        raise ValidationError(errors)
    return True


# ---------------------------------------------------------------------------
# 迁移
# ---------------------------------------------------------------------------
def _migrate_impl(save, target_version):
    source_version = _require_version(save)

    if not _is_int(target_version):
        raise SaveFormatError(
            '目标版本类型错误：期望 int，实际 %s' % type(target_version).__name__
        )
    if target_version < MIN_VERSION:
        raise SaveFormatError(
            "不支持的目标版本 %r：版本号必须 >= %d" % (target_version, MIN_VERSION)
        )
    if source_version > LATEST_VERSION:
        raise FutureVersionError(
            "存档版本 %d 高于最高支持版本 %d，拒绝迁移"
            % (source_version, LATEST_VERSION)
        )
    if target_version > LATEST_VERSION:
        raise FutureVersionError(
            "目标版本 %d 高于最高支持版本 %d，拒绝迁移"
            % (target_version, LATEST_VERSION)
        )
    if target_version < source_version:
        raise MigrationError(
            "不支持降级：存档版本 %d 高于目标版本 %d" % (source_version, target_version)
        )

    current = copy.deepcopy(save)
    filled = 0

    # 先按源版本补默认值：源存档缺字段时也能得到确定结果。
    filled += _fill_defaults(current, SCHEMAS[source_version])

    # 逐版本向前走，每一步只走一个版本。
    while current["version"] < target_version:
        step = _MIGRATION_STEPS[current["version"]]
        current, step_filled = step(current)
        filled += step_filled

    # 迁移结果必须通过目标版本 schema 的校验。
    validate(current, SCHEMAS[target_version])
    return current, filled, source_version


def migrate(save, target_version):
    """把 ``save`` 迁移到 ``target_version``。

    * 支持跨多个版本一次迁移（内部按单步链执行）。
    * 幂等：对已经是目标版本的存档再次迁移，结果与原存档相等。
    * 不修改传入的 ``save``。
    * 未来版本、降级、损坏 / 空存档都会抛出明确的异常。

    返回迁移后的新字典。
    """
    result, _filled, _source = _migrate_impl(save, target_version)
    return result


def migrate_with_report(save, target_version):
    """与 :func:`migrate` 相同，但额外返回迁移统计（供 CLI 使用）。

    返回 ``(迁移结果, 报告)``，报告字段：

    * ``source_version`` / ``target_version``
    * ``steps``：实际执行的单步迁移次数
    * ``filled_fields``：整条链上补齐的字段数（只统计原本缺失的字段）
    * ``unknown_fields``：结果中目标 schema 未声明、被原样保留的字段数
    * ``validation``：固定为 ``"通过"``（失败会直接抛异常）
    """
    result, filled, source_version = _migrate_impl(save, target_version)
    schema = SCHEMAS[target_version]
    unknown = [name for name in result if name not in schema.fields]
    report = {
        "source_version": source_version,
        "target_version": target_version,
        "steps": target_version - source_version,
        "filled_fields": filled,
        "unknown_fields": len(unknown),
        "unknown_field_names": sorted(unknown),
        "validation": "通过",
    }
    return result, report


# ---------------------------------------------------------------------------
# 读写
# ---------------------------------------------------------------------------
def load_save(path):
    """读取并解析存档文件。

    * 空文件 / 空 JSON 对象 -> :class:`EmptySaveError`
    * 非 UTF-8 或非法 JSON -> :class:`CorruptSaveError`
    * 根节点不是对象、缺少 ``version`` 或版本号非法 -> :class:`SaveFormatError`

    未来版本的存档可以被加载（便于检查），但 ``migrate`` 会拒绝它。
    """
    name = os.fspath(path)
    try:
        with open(name, "rb") as fh:
            raw = fh.read()
    except FileNotFoundError:
        raise SaveFormatError("存档文件不存在：%s" % name) from None
    except OSError as exc:
        raise SaveFormatError("无法读取存档文件：%s（%s）" % (name, exc)) from exc

    if not raw.strip():
        raise EmptySaveError("存档为空：%s 不含任何内容" % name)

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CorruptSaveError("存档编码损坏：%s（%s）" % (name, exc)) from None

    if text.startswith("﻿"):
        text = text.lstrip("﻿")

    try:
        save = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CorruptSaveError(
            "存档 JSON 损坏：%s（第 %d 行第 %d 列：%s）"
            % (name, exc.lineno, exc.colno, exc.msg)
        ) from None

    if not isinstance(save, dict):
        raise CorruptSaveError(
            "存档根节点必须是 JSON 对象，实际为 %s：%s" % (type(save).__name__, name)
        )
    if not save:
        raise EmptySaveError("存档为空：%s 中的 JSON 对象不含任何字段" % name)
    _require_version(save, where="存档文件 %s " % name)
    return save


def dumps_save(save):
    """把存档序列化成稳定文本：键排序、UTF-8、LF 换行。

    只要内容相同，输出就逐字节相同。
    """
    return json.dumps(save, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def write_save(save, path):
    """以确定性格式写出存档。"""
    name = os.fspath(path)
    with open(name, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(dumps_save(save))
