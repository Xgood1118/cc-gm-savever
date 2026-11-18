# -*- coding: utf-8 -*-
"""存档版本迁移"""

from dataclasses import dataclass


@dataclass
class Schema:
    """版本 schema。"""
    version: int
    fields: dict




def load_save(path):
    """读存档"""
    raise NotImplementedError("load_save 尚未实现")


def migrate(save, target_version):
    """迁移"""
    raise NotImplementedError("migrate 尚未实现")


def validate(save, schema):
    """校验"""
    raise NotImplementedError("validate 尚未实现")



def merge_patches(base, patches):
    """合并字段级补丁：返回 dict(merged, conflicts)，conflicts 为字段路径列表。"""
    raise NotImplementedError("merge_patches 尚未实现")


def migrate_atomic(save, target_version):
    """原子迁移：返回 dict(ok, save, failed_step, reason)；失败时 save 不变。"""
    raise NotImplementedError("migrate_atomic 尚未实现")
