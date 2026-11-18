# 存档版本迁移

一个能把旧版本存档迁移到新格式、并在字段缺失时给出确定默认值的迁移器。
只使用 Python 标准库，`data/save_v1.json` 为只读输入，不会被修改。

## 保留签名

- `Schema(version, fields)`
- `load_save(path)`
- `migrate(save, target_version)`
- `validate(save, schema)`

另外提供 `migrate_with_report` / `migrate_atomic` / `merge_patches` /
`MIGRATION_STEPS` / `SCHEMAS` / `dumps_save` / `write_save`。

## 工作方式

- **单步迁移链**：每个版本只有一个 `vN -> vN+1` 的单步函数，注册在
  `MIGRATION_STEPS` 中；`migrate` 按顺序把它们串起来，因此
  `migrate(save, 3)` 一次跨多个版本，结果与逐版执行完全一致
  （`migrate(migrate(s, 2), 3) == migrate(s, 3)`）。
- **幂等**：迁移只补缺失字段、只做显式格式升级，不删除任何字段；对已经是目标
  版本的存档再次迁移，结果不变。
- **固定默认值**：缺失字段一律按下表补齐，不读取时间、随机数或环境。
- **未知字段保留**：schema 未声明的字段（如 v1 的 `legacy_flag`）原样保留；
  嵌套结构也原样深拷贝，不会与调用方共享对象。
- **前向引用降级**：旧版存档里如果已经带有只在更高版本才声明的字段，
  到达该版本时：类型合法则保留原值；类型不合法则回退为该字段默认值，
  并在报告的 `downgraded_fields` 中记录，不会因缺字段或类型不对而崩溃。
- **迁移后强制校验**：结果返回前一定按目标 schema 校验；失败抛
  `ValidationError`，其中 `errors` 是 `[(字段名, 原因), ...]`，消息也带字段名与原因。
- **确定性输出**：`dumps_save` / `write_save` 使用 `sort_keys=True`、UTF-8、
  LF 换行；同样的内容两次写出逐字节一致。

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

- 已有字段不会被默认值覆盖；只有原本缺失的字段才计入 `filled_fields`。
- 迁移开始时会先按**源版本** schema 补齐缺失字段，再做源校验；因此 v1 存档
  缺 `legacy_flag` 时会先补成 `False`，随后作为未知字段一路保留。
- 可变的默认值（如 `[]`）每次都会深拷贝，不同调用之间互不影响。
- schema 规格支持三种写法：`{"type": int, "default": 0}`、仅类型 `int`、
  仅默认值 `0`；类型名也可写成 `"int"` / `"str"` / `"list"` 等字符串。

## 补丁合并

`merge_patches(base, patches)` 返回 `{"merged": dict, "conflicts": [字段路径, ...]}`。

- 同一路径被多份补丁写成**相同的值** -> 正常合并；比较是严格的，
  `True` 与 `1`、`1` 与 `1.0` 都不算同值。
- 同一路径出现**不同的值** -> 记录到 `conflicts`，该路径**保留 base 原值**
  （base 没有该字段就保持缺失），绝不静默覆盖。
- 未被任何补丁提及的字段、以及 base 中的未知字段 -> 原样保留。
- 嵌套 dict 表示嵌套字段，冲突路径用点号表示，如 `stats.hp`；
  多个冲突按路径字典序排序，结果确定。
- 结构冲突（同一路径既是叶子又是对象；或 base 在该路径是标量而补丁要写其
  子字段）也计入 `conflicts`，保留 base 原值。
- `base` 与 `patches` 都不会被修改，`merged` 是深拷贝。
  参数类型不对时抛 `TypeError`。

```
>>> merge_patches({"hp": 10}, [{"hp": 12}, {"hp": 12}])
{'merged': {'hp': 12}, 'conflicts': []}
>>> merge_patches({"hp": 10}, [{"hp": 12}, {"hp": 15}])
{'merged': {'hp': 10}, 'conflicts': ['hp']}
```

## 原子迁移

`migrate_atomic(save, target_version)` 返回：

- 成功：`{"ok": True, "save": 结果, "failed_step": None, "reason": None, "report": 报告}`
- 失败：`{"ok": False, "save": 原始存档副本, "failed_step": 失败步骤,
  "reason": 原因, "error_type": 异常类型名, "errors": 明细}`

失败步骤取值：

| `failed_step` | 含义 |
| --- | --- |
| `precheck` | 前置检查失败（空存档、缺 version、未来版本、降级、目标非法） |
| `validate-source@vN` | 源存档按 vN schema 校验失败（损坏字段） |
| `vN->vN+1` | 某个单步迁移执行失败 |
| `validate-target@vN` | 迁移结果按 vN schema 校验失败 |

实现上 `migrate_atomic` 从不修改传入对象，失败时返回的 `save` 是调用前的
深拷贝副本（内容完全一致，且与入参不共享对象）。成功时
`result["save"] == migrate(save, target_version)`，逐字节一致。

## 边界情况

| 情况 | 结果 |
| --- | --- |
| 未来版本存档（`version > 3`） | `load_save` 可读；`migrate` 抛 `FutureVersionError` |
| 目标版本 > 3 | `FutureVersionError` |
| 目标版本低于存档版本（降级） | `MigrationError` |
| 空文件 / 纯空白 / 空 JSON 对象 | `EmptySaveError` |
| 非法 JSON / 非 UTF-8 / 根节点不是对象 | `CorruptSaveError` |
| 缺少 `version`、`version` 非 int、版本号 < 1 | `SaveFormatError` |
| 存档字段类型错误 | `ValidationError`（带字段名与原因） |
| 文件不存在 / 无法读取 | `SaveFormatError` |

所有异常都继承 `SaveError`（同时是 `ValueError`）。
`migrate_atomic` 把这些失败一律转换成 `ok=False` 的返回值，且内容确定可复现。

## 自测

```
python -m pytest tests -q
python -m savever.cli data/save_v1.json --to 3 --out save_v3.json
python -m savever.cli data/save_v1.json --to 3 --out save_v3.json --atomic
```

测试覆盖：迁移链（直连 == 逐步）、幂等、默认值、未知字段保留、
报告统计、校验报错字段名、未来版本 / 损坏 / 空存档、两次输出逐字节一致、
补丁合并（同值/异值/嵌套/结构冲突/不可变/确定性）、原子迁移（步骤与原因、
源不变、与普通迁移一致、步骤内异常）、前向引用降级、CLI（成功/失败/原子/帮助）。

## 实测结果

`python -m savever.cli data/save_v1.json --to 3 --out save_v3.json` 输出：

```
存档：data/save_v1.json -> save_v3.json
版本：v1 -> v3
迁移步数：2（v1->v2、v2->v3）
补齐字段数：4（hp、inventory、mana、difficulty）
降级字段数：0
保留未知字段数：1（legacy_flag）
校验结果：通过（v3 schema）
```

- **迁移步数**：2（`v1 -> v2`、`v2 -> v3`）
- **补齐字段数**：4（v2 补 `hp`、`inventory`；v3 补 `mana`、`difficulty`）
- **降级字段数**：0
- **保留未知字段数**：1（`legacy_flag`）
- **校验结果**：通过（按 v3 schema）

生成的 `save_v3.json`：

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

- 连续两次运行 CLI，输出文件逐字节一致：
  SHA-256 `b13b7a8306975b3e473d9d0f344833bdc9b77eaf947a2f1aca6191ef0afbb0cf`，128 字节。
- `--atomic` 成功路径输出与普通路径完全相同（`cmp` 无差异）。

其它验收数据：

- `migrate_atomic({"version": 4, "gold": 1}, 4)` ->
  `ok=False, failed_step="precheck"`，源存档不变。
- 前向引用降级示例 `{"version": 1, "gold": 1, "hp": "bad"}` 迁到 v3 ->
  `hp` 回落为 `100`，`downgraded_fields == ["hp"]`。
- `merge_patches({"version": 3, "stats": {"hp": 10}},
  [{"stats": {"hp": 12}}, {"stats": {"hp": 15}}])` ->
  `conflicts == ["stats.hp"]`，`merged["stats"]["hp"] == 10`。

## 扩展新版本

1. 在 `SCHEMAS` 中新增 `Schema(4, {...})`，并同步上面的默认值表。
2. 新增单步函数 `_migrate_3_to_4(save)`（内部调用 `_advance_one(save, 3, 4)`），
   注册到 `MIGRATION_STEPS[3]`。
3. 把 `LATEST_VERSION` 改为 `4`。
4. 在 `tests/` 中补链式迁移、幂等与前向引用降级用例。
