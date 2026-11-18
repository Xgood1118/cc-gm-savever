# 存档版本迁移

一个能把旧版本存档迁移到新格式、并在字段缺失时给出确定默认值的迁移器

## 保留签名

- `Schema`
- `load_save(path)`
- `migrate(save, target_version)`
- `validate(save, schema)`

## 自测

```
python -m pytest tests -q
python -m savever.cli data/save_v1.json --to 3 --out save_v3.json
```
