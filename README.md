# 存档版本迁移

一个能把旧版本存档迁移到新格式、并在字段缺失时给出确定默认值的迁移器。
只使用 Python 标准库。

## 保留签名

- `Schema(version, fields)`
- `load_save(path)`
- `migrate(save, target_version)`
- `validate(save, schema)`

## 工作方式

- **单步迁移链**：每个版本只走一步（`v1 -> v2`、`v2 -> v3`），`migrate` 按顺序串起来，
  因此 `migrate(save, 3)` 可以直接把 v1 存档迁到 v3，结果与手工逐步迁移完全一致。
- **幂等**：迁移只补缺失字段、只改写格式，不删除任何字段；对已经是目标版本的存档
  再迁移一次，结果与原来相等。
- **未知字段保留**：schema 未声明的字段（如 v1 的 `legacy_flag`）在每个步骤中原样保留。
- **固定默认值**：缺失字段按下表补齐，不会用时间、随机数等不确定来源。
- **迁移后强制校验**：`migrate` 返回前会按目标 schema 调用 `validate`；校验失败抛出
  `ValidationError`，其中 `errors` 是 `[(字段名, 原因), ...]`，消息里也带字段名与原因。
- **确定性输出**：`dumps_save` / `write_save` 使用 `sort_keys=True`、UTF-8、LF 换行，
  同样内容两次写出逐字节一致。

## 默认值表

| 版本 | 字段 | 类型 | 默认值 |
| --- | --- | --- | --- |
| 1 | `version` | int | `1` |
| 1 | `gold` | int | `0` |
| 1 | `legacy_flag` | bool | `False` |
| 2 | `version` | int | `2` |
| 2 | `gold` | int | `0` |
| 2 | `hp` | int | `100` |
| 2 | `inventory` | list | `[]` |
| 3 | `version` | int | `3` |
| 3 | `gold` | int | `0` |
| 3 | `hp` | int | `100` |
| 3 | `mana` | int | `30` |
| 3 | `difficulty` | str | `"normal"` |
| 3 | `inventory` | list | `[]` |

补充说明：

- 迁移到 v2 时，v1 的 `legacy_flag` 在 v2 schema 中没有声明，因此作为未知字段保留到最终结果。
- 已有字段不会被默认值覆盖；只有原本缺失的字段才计入"补齐字段数"。
- 补齐在链上逐步进行：v1 存档缺 `legacy_flag` 时，会先按 v1 的默认值补成 `False`，
  之后作为未知字段一路保留。

## 边界情况

| 情况 | 结果 |
| --- | --- |
| 未来版本存档（`version > 3`） | `load_save` 可以读入；`migrate` 抛 `FutureVersionError` 拒绝 |
| 目标版本超过 3 | `FutureVersionError` |
| 目标版本低于存档版本（降级） | `MigrationError` |
| 空文件 / 空 JSON 对象 | `EmptySaveError`（`load_save` 与 `migrate` 都会抛） |
| 非法 JSON / 非 UTF-8 / 根节点不是对象 | `CorruptSaveError` |
| 缺少 `version`、`version` 非 int、版本号 < 1 | `SaveFormatError` |
| 文件不存在 / 无法读取 | `SaveFormatError` |

所有异常都继承自 `SaveError`，而 `SaveError` 继承 `ValueError`，按 `ValueError` 捕获也可。
`ValidationError` 额外带 `field` / `reason` / `errors` 属性。

## 自测

```
python -m pytest tests -q
python -m savever.cli data/save_v1.json --to 3 --out save_v3.json
```

测试覆盖：多版本链式迁移（直连 == 逐步）、幂等、默认值补齐、未知字段保留、
未来版本拒绝、损坏/空存档、校验报错字段名、以及两次输出逐字节一致。

## 实测结果

`python -m savever.cli data/save_v1.json --to 3 --out save_v3.json` 输出：

```
存档：data/save_v1.json -> save_v3.json
版本：v1 -> v3
迁移步数：2
补齐字段数：4
保留未知字段数：1（legacy_flag）
校验结果：通过（v3 schema）
```

- **迁移步数**：2（`v1 -> v2`、`v2 -> v3`）
- **补齐字段数**：4（v2 补 `hp`、`inventory`；v3 补 `mana`、`difficulty`）
- **保留未知字段数**：1（`legacy_flag`，v3 schema 未声明，原样保留）
- **校验结果**：通过（按 v3 schema）

生成 `save_v3.json` 的内容：

```json
{
  "difficulty": "normal",
  "gold": 10,
  "hp": 100,
  "inventory": [],
  "legacy_flag": true,
  "mana": 30,
  "version": 3
}
```

连续两次迁移写出的文件 SHA-256 一致：
`b13b7a8306975b3e473d9d0f344833bdc9b77eaf947a2f1aca6191ef0afbb0cf`（128 字节）。

## 扩展新版本

1. 在 `SCHEMAS` 中新增 `Schema(4, {...})`，并同步上面的默认值表。
2. 新增单步函数 `_migrate_3_to_4(save)`（只做 `3 -> 4`），注册到 `_MIGRATION_STEPS`。
3. 把 `LATEST_VERSION` 改为 `4`。
4. 在 `tests/` 中补一条链式迁移与幂等用例。

`data/save_v1.json` 为只读输入数据，不会被迁移器修改。
