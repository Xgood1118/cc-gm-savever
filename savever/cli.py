# -*- coding: utf-8 -*-
"""命令行入口。

用法::

    python -m savever.cli <存档> --to <版本> --out <输出>
    python -m savever.cli <存档> --to <版本> --out <输出> --atomic
"""

import argparse
import sys

import savever as m


def _build_parser():
    ap = argparse.ArgumentParser(description="存档版本迁移")
    ap.add_argument("input", help="输入存档路径")
    ap.add_argument("--to", type=int, required=True, help="目标版本")
    ap.add_argument("--out", required=True, help="输出存档路径")
    ap.add_argument(
        "--atomic",
        action="store_true",
        help="使用原子迁移；失败时不写出任何文件",
    )
    return ap


def _print_report(source, out_path, report):
    print("存档：%s -> %s" % (source, out_path))
    print(
        "版本：v%d -> v%d"
        % (report["source_version"], report["target_version"])
    )
    print("迁移步数：%d（%s）" % (report["step_count"], "、".join(report["steps"]) or "无"))
    print(
        "补齐字段数：%d%s"
        % (
            report["filled_fields"],
            ("（%s）" % "、".join(report["filled_field_names"]))
            if report["filled_field_names"]
            else "",
        )
    )
    print(
        "降级字段数：%d%s"
        % (
            len(report["downgraded_fields"]),
            ("（%s）" % "、".join(report["downgraded_fields"]))
            if report["downgraded_fields"]
            else "",
        )
    )
    print(
        "保留未知字段数：%d%s"
        % (
            report["unknown_fields"],
            ("（%s）" % "、".join(report["unknown_field_names"]))
            if report["unknown_field_names"]
            else "",
        )
    )
    print("校验结果：%s（v%d schema）" % (report["validation"], report["target_version"]))


def main(argv=None):
    ap = _build_parser()
    args = ap.parse_args(argv)

    try:
        save = m.load_save(args.input)
    except m.SaveError as exc:
        print("读取失败：%s" % exc, file=sys.stderr)
        return 1

    if args.atomic:
        outcome = m.migrate_atomic(save, args.to)
        if not outcome["ok"]:
            print(
                "迁移失败（步骤 %s）：%s"
                % (outcome["failed_step"], outcome["reason"]),
                file=sys.stderr,
            )
            return 1
        result, report = outcome["save"], outcome["report"]
    else:
        try:
            result, report = m.migrate_with_report(save, args.to)
        except m.SaveError as exc:
            print("迁移失败：%s" % exc, file=sys.stderr)
            return 1

    try:
        m.write_save(result, args.out)
    except OSError as exc:
        print("写出失败：%s" % exc, file=sys.stderr)
        return 1

    _print_report(args.input, args.out, report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
