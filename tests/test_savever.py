# -*- coding: utf-8 -*-
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import savever as mod  # noqa: E402

def read_text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def read_hex(path):
    with open(path, encoding="utf-8") as f:
        return f.read().strip()


def read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def test_migrate_to_v3():
    s = savever.load_save("data/save_v1.json")
    out = savever.migrate(s, 3)
    assert out["version"] == 3


def test_idempotent():
    s = savever.load_save("data/save_v1.json")
    a = savever.migrate(s, 3)
    b = savever.migrate(a, 3)
    assert a == b
