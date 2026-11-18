# -*- coding: utf-8 -*-
"""存档版本迁移。

设计要点
--------

* **单步迁移链**：每个版本只提供一个 ``vN -> vN+1`` 的单步函数，``migrate``
  把这些函数按顺序串起来，因此可以一次跨多个版本，且结果与逐版执行完全一致。
* **幂等**：迁移只补缺失字段、只做显式格式升级，不删除字段。已经处于目标
  版本的存档再次迁移时结果不变。
* **固定默认值**：缺失字段一律按 ``SCHEMAS`` 中的固定默认值补齐，默认值不会
  读取时间、随机数或环境，保证结果可复现。
* **未知字段保留**：目标 schema 未声明的字段原样保留，不会被丢弃。
* **前向引用降级**：旧版存档里如果已经带有只在更高版本才声明的字段（例如
  v1 存档里出现 v2 的 ``hp``），到达该版本时：类型合法则保留；类型不合法则
  回退为该字段的默认值并记录为 downgrade，而不是直接崩溃。
* **迁移后强制校验**：迁移结果返回前一定按目标 schema 校验，失败时异常里
  逐项给出字段名与原因。
* **确定性输出**：``dumps_save`` / ``write_save`` 按键排序、UTF-8、LF 换行，
  相同内容两次写出逐字节一致。
* **原子迁移**：``migrate_atomic`` 在失败时返回原始存档的一份副本（内容与
  调用前完全一致），并给出失败步骤与原因；成功结果与 ``migrate`` 相等。
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
    "MIGRATION_STEPS",
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
    "migrate_atomic",
    "validate",
    "merge_patches",
    "dumps_save",
    "write_save",
]


MIN_VERSION = 1
LATEST_VERSION = 3

_MISSING = object()


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------
class SaveError(ValueError):
    """存档相关错误的基类（同时是 ``ValueError``，便于调用方统一捕获）。"""


class SaveFormatError(SaveError):
    """存档结构不合法（缺少 version、字段类型错误等）。"""


class EmptySaveError(SaveFormatError):
    """空存档：空文件，或 JSON 对象不含任何字段。"""


class CorruptSaveError(SaveFormatError):
    """存档内容损坏：无法解码、非法 JSON、根节点不是对象。"""


class FutureVersionError(SaveError):
    """存档版本或目标版本超出当前支持范围。"""


class MigrationError(SaveError):
    """迁移方向不合法（例如要求降级），或某个迁移步骤执行失败。"""


class ValidationError(SaveError):
    """校验失败。

    ``errors`` 是 ``[(字段名, 原因), ...]``，消息里也会带字段名与原因。
    """

    def __init__(self, errors, message=None):
        self.errors = [(str(name), str(reason)) for name, reason in errors]
        self.field = self.errors[0][0] if self.errors else None
        self.reason = self.errors[0][1] if self.errors else None
        if message is None:
            message = "校验失败：" + "；".join(
                '字段 "%s" %s' % (name, reason) for name, reason in self.errors
            )
        super().__init__(message)


class _MigrationFailure(Exception):
    """内部使用：给迁移失败打上“失败步骤”的标记。"""

    def __init__(self, step, cause):
        self.step = step
        self.cause = cause
        super().__init__("%s：%s" % (step, cause))


# ---------------------------------------------------------------------------
# Schema 与默认值表
# ---------------------------------------------------------------------------
@dataclass
class Schema:
    """版本 schema。

    ``fields`` 是 ``{字段名: 规格}``。规格支持三种写法：

    * 完整形式：``{"type": int, "default": 0}``
    * 只声明类型：``int``（默认值由类型推断：int -> 0、str -> "" 等）
    * 只给出默认值：``0`` / ``[]`` / ``"normal"``（类型由值的类型推断）

    类型名也接受字符串，例如 ``"int"``、``"str"``、``"list"``。
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
# 类型 / 规格工具
# ---------------------------------------------------------------------------
_TYPE_NAMES = {
    "int": int,
    "integer": int,
    "str": str,
    "string": str,
    "bool": bool,
    "boolean": bool,
    "list": list,
    "array": list,
    "dict": dict,
    "object": dict,
    "float": float,
    "number": float,
    "none": type(None),
    "null": type(None),
    "any": None,
}


def _is_int(value):
    """严格判断 int：``bool`` 是 ``int`` 的子类，这里必须排除。"""
    return isinstance(value, int) and not isinstance(value, bool)


def _resolve_type(expected):
    if expected is None:
        return None
    if isinstance(expected, str):
        key = expected.strip().lower()
        if key not in _TYPE_NAMES:
            raise SaveFormatError("schema 中未知的类型名：%r" % expected)
        return _TYPE_NAMES[key]
    if isinstance(expected, tuple):
        return tuple(_resolve_type(item) for item in expected)
    if isinstance(expected, type):
        return expected
    raise SaveFormatError("schema 中非法的类型声明：%r" % (expected,))


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
    if expected is bool:
        return type(value) is bool
    return isinstance(value, expected)


def _default_for_type(expected):
    if expected is None:
        return None
    if isinstance(expected, tuple):
        return _default_for_type(expected[0])
    return {
        int: 0,
        float: 0.0,
        str: "",
        bool: False,
        list: list,
        dict: dict,
        type(None): None,
    }.get(expected, None)


def _spec_type(spec):
    """从规格中解析出类型（None 表示任意类型）。"""
    if isinstance(spec, dict):
        if "type" in spec:
            return _resolve_type(spec["type"])
        if "default" in spec:
            return type(spec["default"])
        return None
    if isinstance(spec, tuple) and spec and all(
        isinstance(item, (str, type)) for item in spec
    ):
        return _resolve_type(spec)
    if isinstance(spec, type):
        return spec
    return type(spec)


def _spec_default(spec):
    """从规格中解析出默认值（每次返回独立副本，避免共享可变对象）。"""
    if isinstance(spec, dict):
        if "default" in spec:
            return copy.deepcopy(spec["default"])
        factory = spec.get("default_factory")
        if callable(factory):
            return factory()
        if "type" in spec:
            return _default_for_type(_resolve_type(spec["type"]))
        return None
    if isinstance(spec, tuple) and spec and all(
        isinstance(item, (str, type)) for item in spec
    ):
        return _default_for_type(_resolve_type(spec))
    if isinstance(spec, type):
        return _default_for_type(spec)
    return copy.deepcopy(spec)


def _fill_defaults(save, schema):
    """给缺失字段补固定默认值，返回被补齐的字段名列表（schema 顺序）。

    已有字段（包括 schema 未声明的未知字段）一律不动。
    """
    filled = []
    for name, spec in schema.fields.items():
        if name not in save:
            save[name] = _spec_default(spec)
            filled.append(name)
    return filled


# ---------------------------------------------------------------------------
# 版本字段与前置检查
# ---------------------------------------------------------------------------
def _require_version_field(save, where="存档"):
    """检查 version 字段的存在与基本类型，返回版本号（允许未来版本）。"""
    if not isinstance(save, dict):
        raise SaveFormatError(
            "%s必须是 JSON 对象，实际为 %s" % (where, type(save).__name__)
        )
    if "version" not in save:
        raise SaveFormatError('%s缺少 "version" 字段' % where)
    version = save["version"]
    if not _is_int(version):
        raise SaveFormatError(
            '字段 "version" 类型错误：期望 int，实际 %s'
            % type(version).__name__
        )
    if version < MIN_VERSION:
        raise SaveFormatError(
            "不支持的存档版本 %r：版本号必须 >= %d" % (version, MIN_VERSION)
        )
    return version


def _precheck(save, target_version):
    """迁移前置检查：结构、源版本、目标版本、迁移方向。"""
    if not isinstance(save, dict):
        raise SaveFormatError(
            "存档必须是 JSON 对象，实际为 %s" % type(save).__name__
        )
    if not save:
        raise EmptySaveError("存档为空：JSON 对象不含任何字段")
    source_version = _require_version_field(save)

    if not _is_int(target_version):
        raise SaveFormatError(
            "目标版本类型错误：期望 int，实际 %s" % type(target_version).__name__
        )
    if target_version < MIN_VERSION:
        raise SaveFormatError(
            "不支持的目标版本 %r：版本号必须 >= %d"
            % (target_version, MIN_VERSION)
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
            "不支持降级：存档版本 %d 高于目标版本 %d"
            % (source_version, target_version)
        )
    return source_version


# ---------------------------------------------------------------------------
# 单步迁移：每个函数只负责 version -> version + 1
# ---------------------------------------------------------------------------
def _advance_one(save, from_version, to_version):
    """把存档向前推进恰好一个版本。

    返回 ``(新存档, 补齐字段名列表, 降级字段名列表)``。

    对于“目标版本新声明、源版本尚未声明”的字段（前向引用字段）：
    已存在且类型合法 -> 保留；已存在但类型不合法 -> 回退默认值并记录降级。
    """
    from_schema = SCHEMAS[from_version]
    to_schema = SCHEMAS[to_version]

    result = copy.deepcopy(save)
    result["version"] = to_version

    downgraded = []
    for name, spec in to_schema.fields.items():
        if name == "version":
            continue
        if name in from_schema.fields:
            continue
        if name not in result:
            continue
        expected = _spec_type(spec)
        if not _type_ok(result[name], expected):
            result[name] = _spec_default(spec)
            downgraded.append(name)

    filled = _fill_defaults(result, to_schema)
    return result, filled, downgraded


def _migrate_1_to_2(save):
    return _advance_one(save, 1, 2)


def _migrate_2_to_3(save):
    return _advance_one(save, 2, 3)


#: 单步迁移表：键是源版本，函数只做 ``version -> version + 1``。
MIGRATION_STEPS = {
    1: _migrate_1_to_2,
    2: _migrate_2_to_3,
}


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------
def _as_schema(schema):
    if isinstance(schema, Schema):
        return schema
    if _is_int(schema):
        if schema not in SCHEMAS:
            raise SaveFormatError("未知的 schema 版本：%r" % (schema,))
        return SCHEMAS[schema]
    raise SaveFormatError(
        "schema 必须是 Schema 实例或已知版本号，实际为 %s"
        % type(schema).__name__
    )


def validate(save, schema):
    """按 ``schema`` 校验存档。

    成功返回 ``True``；失败抛出 :class:`ValidationError`，其中 ``errors``
    是 ``[(字段名, 原因), ...]``，异常消息也包含字段名与原因。

    schema 未声明的字段视为未知字段，允许存在（迁移器会保留它们）。
    """
    schema = _as_schema(schema)

    if not isinstance(save, dict):
        raise ValidationError(
            [("<存档>", "必须是 JSON 对象，实际为 %s" % type(save).__name__)]
        )

    errors = []

    version_expected = schema.version
    if "version" not in save:
        errors.append(("version", "缺少字段"))
    elif not _is_int(save["version"]):
        errors.append(
            (
                "version",
                "类型错误：期望 int，实际 %s" % type(save["version"]).__name__,
            )
        )
    elif save["version"] != version_expected:
        errors.append(
            ("version", "版本不匹配：期望 %d，实际 %r" % (version_expected, save["version"]))
        )

    for name, spec in (schema.fields or {}).items():
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
def _migrate_core(save, target_version):
    """执行迁移并返回 ``(结果, 报告)``；失败时抛出 ``_MigrationFailure``。"""
    try:
        source_version = _precheck(save, target_version)
    except SaveError as exc:
        raise _MigrationFailure("precheck", exc) from exc

    current = copy.deepcopy(save)
    current["version"] = source_version

    # 源版本先按自身 schema 补齐缺失字段，再做源校验：
    # 缺失字段不算错误；已声明字段类型错误则判定为损坏存档。
    filled_names = _fill_defaults(current, SCHEMAS[source_version])
    try:
        validate(current, SCHEMAS[source_version])
    except SaveError as exc:
        raise _MigrationFailure(
            "validate-source@v%d" % source_version, exc
        ) from exc

    steps = []
    downgraded = []

    while current["version"] < target_version:
        from_version = current["version"]
        to_version = from_version + 1
        step_name = "v%d->v%d" % (from_version, to_version)
        step = MIGRATION_STEPS.get(from_version)
        if step is None:
            raise _MigrationFailure(
                step_name,
                MigrationError(
                    "没有从 v%d 到 v%d 的迁移步骤" % (from_version, to_version)
                ),
            )
        try:
            current, step_filled, step_downgraded = step(current)
        except SaveError as exc:
            raise _MigrationFailure(step_name, exc) from exc
        except Exception as exc:  # noqa: BLE001 - 任何步骤异常都要转成可报告的失败
            raise _MigrationFailure(
                step_name,
                MigrationError("迁移步骤 %s 执行失败：%s" % (step_name, exc)),
            ) from exc

        steps.append(step_name)
        filled_names.extend(step_filled)
        downgraded.extend(step_downgraded)

    try:
        validate(current, SCHEMAS[target_version])
    except SaveError as exc:
        raise _MigrationFailure(
            "validate-target@v%d" % target_version, exc
        ) from exc

    target_schema = SCHEMAS[target_version]
    unknown = [name for name in current if name not in target_schema.fields]
    report = {
        "source_version": source_version,
        "target_version": target_version,
        "steps": steps,
        "step_count": len(steps),
        "filled_fields": len(filled_names),
        "filled_field_names": list(filled_names),
        "downgraded_fields": list(downgraded),
        "unknown_fields": len(unknown),
        "unknown_field_names": sorted(unknown),
        "validation": "通过",
    }
    return current, report


def migrate(save, target_version):
    """把 ``save`` 迁移到 ``target_version`` 并返回新字典。

    * 跨多个版本时内部按单步链执行，结果与逐版迁移一致；
    * 幂等：对已是目标版本的存档再次迁移，结果不变；
    * 不修改传入的 ``save``；
    * 迁移结果保证通过目标 schema 的 :func:`validate`；
    * 未来版本、降级、损坏 / 空存档都会抛出 ``SaveError`` 子类。
    """
    try:
        result, _report = _migrate_core(save, target_version)
    except _MigrationFailure as failure:
        raise failure.cause
    return result


def migrate_with_report(save, target_version):
    """与 :func:`migrate` 相同，但额外返回迁移报告。

    报告字段：

    * ``source_version`` / ``target_version``
    * ``steps``：实际执行的单步迁移名列表，如 ``["v1->v2", "v2->v3"]``
    * ``step_count``：单步迁移次数
    * ``filled_fields`` / ``filled_field_names``：补齐的字段数与字段名
    * ``downgraded_fields``：因前向引用类型不合法而回落默认值的字段名
    * ``unknown_fields`` / ``unknown_field_names``：目标 schema 未声明、被保留的字段
    * ``validation``：固定为 ``"通过"``（失败会直接抛异常）
    """
    try:
        result, report = _migrate_core(save, target_version)
    except _MigrationFailure as failure:
        raise failure.cause
    return result, report


def migrate_atomic(save, target_version):
    """原子迁移。

    返回 dict：

    * 成功：``{"ok": True, "save": 迁移结果, "failed_step": None,
      "reason": None, "report": 报告}``
    * 失败：``{"ok": False, "save": 原始存档副本, "failed_step": 失败步骤,
      "reason": 失败原因, "error_type": 异常类型名, "errors": 明细}``

    失败时返回的 ``save`` 与调用前内容完全一致；实现上从不修改传入对象。
    成功结果与 ``migrate(save, target_version)`` 相等。
    """
    snapshot = copy.deepcopy(save)

    try:
        result, report = _migrate_core(save, target_version)
    except _MigrationFailure as failure:
        return {
            "ok": False,
            "save": snapshot,
            "failed_step": failure.step,
            "reason": str(failure.cause),
            "error_type": type(failure.cause).__name__,
            "errors": list(getattr(failure.cause, "errors", [])),
        }

    return {
        "ok": True,
        "save": result,
        "failed_step": None,
        "reason": None,
        "report": report,
    }


# ---------------------------------------------------------------------------
# 字段级补丁合并
# ---------------------------------------------------------------------------
def _values_equal(left, right):
    """严格比较两个 JSON 值：bool 与 int、int 与 float 都不视为同值。"""
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return type(left) is type(right) and left == right
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            _values_equal(left[key], right[key]) for key in left
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _values_equal(a, b) for a, b in zip(left, right)
        )
    return left == right


def _path_text(path):
    return ".".join(str(part) for part in path)


def _get_path(data, path):
    node = data
    for part in path:
        if not isinstance(node, dict) or part not in node:
            return _MISSING
        node = node[part]
    return node


def _set_path(data, path, value):
    node = data
    for part in path[:-1]:
        child = node.get(part, _MISSING)
        if child is _MISSING:
            child = {}
            node[part] = child
        node = child
    node[path[-1]] = value


def _path_has_conflict_ancestor(path, conflicts):
    return any(path[:index] in conflicts for index in range(1, len(path)))


def merge_patches(base, patches):
    """合并多份字段级补丁。

    ``base`` 是原始存档，``patches`` 是若干 dict。嵌套 dict 表示嵌套字段，
    例如 ``{"stats": {"hp": 1}}`` 对应字段路径 ``stats.hp``。

    返回 ``{"merged": dict, "conflicts": [字段路径, ...]}``：

    * 同一路径被多份补丁写成**相同值** -> 正常合并；
    * 同一路径出现**不同值** -> 记入 ``conflicts``，该路径保留 ``base`` 原值
      （base 没有该字段时不写入），绝不静默覆盖；
    * 未被任何补丁提及的字段、以及 base 中的未知字段 -> 原样保留；
    * 结构冲突（一个补丁把某路径当叶子，另一个补丁把它当对象；或 base 在该
      路径是标量而补丁要写其子字段）-> 记入 ``conflicts``，保留 base 原值。

    ``conflicts`` 按路径字典序排序，保证结果确定。``base`` 与 ``patches``
    都不会被修改。
    """
    if not isinstance(base, dict):
        raise TypeError("base 必须是 dict，实际为 %s" % type(base).__name__)
    if isinstance(patches, dict) or not hasattr(patches, "__iter__"):
        raise TypeError("patches 必须是补丁 dict 的序列")
    patches = list(patches)

    proposals = {}      # path tuple -> [value, ...]（叶子写入值）
    dict_paths = set()  # 补丁按“对象”方式触碰过的路径
    for patch in patches:
        if not isinstance(patch, dict):
            raise TypeError(
                "每个补丁必须是 dict，实际为 %s" % type(patch).__name__
            )

        stack = [((), patch)]
        while stack:
            prefix, node = stack.pop()
            for key, value in node.items():
                path = prefix + (key,)
                if isinstance(value, dict):
                    dict_paths.add(path)
                    stack.append((path, value))
                else:
                    proposals.setdefault(path, []).append(copy.deepcopy(value))

    conflicts = set()

    # 同一路径既被当作叶子又被当作对象 -> 结构冲突。
    for path in set(proposals) & dict_paths:
        conflicts.add(path)

    # 补丁要写子字段，但 base 在该路径不是对象 -> 结构冲突，保留 base 原值。
    for path in dict_paths:
        base_value = _get_path(base, path)
        if base_value is not _MISSING and not isinstance(base_value, dict):
            conflicts.add(path)

    # 同一路径的不同写入值 -> 冲突。
    for path, values in proposals.items():
        first = values[0]
        if any(not _values_equal(first, other) for other in values[1:]):
            conflicts.add(path)

    merged = copy.deepcopy(base)

    for path in sorted(proposals):
        if path in conflicts or _path_has_conflict_ancestor(path, conflicts):
            continue
        _set_path(merged, path, copy.deepcopy(proposals[path][0]))

    return {
        "merged": merged,
        "conflicts": sorted(_path_text(path) for path in conflicts),
    }


# ---------------------------------------------------------------------------
# 读写与确定性序列化
# ---------------------------------------------------------------------------
def load_save(path):
    """读取并解析存档文件。

    * 空文件 / 只含空白的文件 / 空 JSON 对象 -> :class:`EmptySaveError`
    * 非 UTF-8、非法 JSON、根节点不是对象 -> :class:`CorruptSaveError`
    * 缺少 ``version``、``version`` 非 int、版本号 < 1 -> :class:`SaveFormatError`

    未来版本的存档可以被加载（便于检查），但 ``migrate`` 会以
    :class:`FutureVersionError` 拒绝迁移。
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
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CorruptSaveError(
            "存档编码损坏：%s（%s）" % (name, exc)
        ) from None

    try:
        save = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CorruptSaveError(
            "存档 JSON 损坏：%s（第 %d 行第 %d 列：%s）"
            % (name, exc.lineno, exc.colno, exc.msg)
        ) from None

    if not isinstance(save, dict):
        raise CorruptSaveError(
            "存档根节点必须是 JSON 对象，实际为 %s：%s"
            % (type(save).__name__, name)
        )
    if not save:
        raise EmptySaveError("存档为空：%s 中的 JSON 对象不含任何字段" % name)
    _require_version_field(save, where="存档文件 %s " % name)
    return save


def dumps_save(save):
    """把存档序列化成确定性文本：键排序、UTF-8、LF 换行。

    只要内容相同，输出就逐字节相同。
    """
    return json.dumps(save, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def write_save(save, path):
    """以确定性格式写出存档（UTF-8、LF 换行）。"""
    name = os.fspath(path)
    with open(name, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(dumps_save(save))
