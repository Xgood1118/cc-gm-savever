# -*- coding: utf-8 -*-
"""存档版本迁移"""

from dataclasses import dataclass


@dataclass
class Schema:
    """版本 schema。"""
    version: int
    fields: dict


def Schema:
    """class"""
    raise NotImplementedError("Schema 尚未实现")


def load_save(path):
    """读存档"""
    raise NotImplementedError("load_save 尚未实现")


def migrate(save, target_version):
    """迁移"""
    raise NotImplementedError("migrate 尚未实现")


def validate(save, schema):
    """校验"""
    raise NotImplementedError("validate 尚未实现")

