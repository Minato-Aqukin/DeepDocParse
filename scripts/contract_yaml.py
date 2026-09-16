"""契约 YAML 的唯一读取口：**重复键当场报错，不静默丢内容。**

`yaml.safe_load` 对同一个 mapping 里的重复键静默只保留最后一个。这个项目已经被
这条语义坑过两次：一次是拿重复键做变异、变异根本没生效（见 `CLAUDE.md`），一次是
在 OpenAPI 的同一个操作下写了两段 `description:`，先写的那段整段消失，而所有守卫
都是绿的 —— 路由守卫只比路径，`--check` 只比生成物，没有一个会看正文。

所以契约文件一律走这里读。
"""
from __future__ import annotations

from pathlib import Path

import yaml


class StrictLoader(yaml.SafeLoader):
    """与 SafeLoader 相同，但重复键报错。"""


def _no_duplicate_keys(loader: StrictLoader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping", node.start_mark,
                f"重复键 {key!r}：YAML 会静默只保留最后一个", key_node.start_mark)
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicate_keys)


def load(path: Path | str):
    """读契约 YAML。重复键 -> `::error::` 并退出 1（调用方不必各写一遍这段）。"""
    path = Path(path)
    try:
        return yaml.load(path.read_text(encoding="utf-8"), Loader=StrictLoader)
    except yaml.YAMLError as exc:
        print(f"::error::{path} 不是合法契约：{exc}")
        raise SystemExit(1) from None
