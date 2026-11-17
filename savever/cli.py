# -*- coding: utf-8 -*-
"""命令行入口：python -m savever.cli <存档> --to <版本> --out <输出>"""

import argparse
import sys

import savever as m


def _build_parser():
    ap = argparse.ArgumentParser(description="存档版本迁移")
    ap.add_argument("input", help="输入存档路径")
    ap.add_argument("--to", type=int, required=True, help="目标版本")
    ap.add_argument("--out", required=True, help="输出存档路径")
    return ap


def main(argv=None):
    ap = _build_parser()
    args = ap.parse_args(argv)

    try:
        save = m.load_save(args.input)
        result, report = m.migrate_with_report(save, args.to)
        m.write_save(result, args.out)
    except m.SaveError as exc:
        print("迁移失败：%s" % exc, file=sys.stderr)
        return 1

    print("存档：%s -> %s" % (args.input, args.out))
    print("版本：v%d -> v%d" % (report["source_version"], report["target_version"]))
    print("迁移步数：%d" % report["steps"])
    print("补齐字段数：%d" % report["filled_fields"])
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
