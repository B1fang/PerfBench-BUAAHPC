#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
轻量 .env 加载工具。

设计目标：
- 无额外依赖（不强制 python-dotenv）。
- 支持最常见的 KEY=VALUE / export KEY=VALUE 形式。
- 默认不覆盖已有环境变量；调用方可按需 override。
"""

import os
from typing import Dict, List


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and ((value[0] == '"' and value[-1] == '"') or (value[0] == "'" and value[-1] == "'")):
        value = value[1:-1]
    return value


def load_env_file(path: str, override: bool = False) -> Dict[str, str]:
    """
    从指定 .env 文件加载环境变量。

    返回值：实际写入进程环境的键值对。
    """

    loaded: Dict[str, str] = {}
    if not path or not os.path.isfile(path):
        return loaded

    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export ") :].strip()
            if "=" not in line:
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            value = _strip_quotes(value)
            if not key:
                continue

            if override or key not in os.environ:
                os.environ[key] = value
                loaded[key] = value

    return loaded


def load_perfbench_env(project_root: str = None, override: bool = False) -> Dict[str, List[str]]:
    """
    加载 PerfBench 相关 .env，优先级（后加载优先）：
    1) ~/.perfbench/.env
    2) <project_root>/.env
    3) <cwd>/.env

    返回值：每个文件实际写入的键列表（用于日志展示）。
    """

    loaded_map: Dict[str, List[str]] = {}

    home_env = os.path.expanduser("~/.perfbench/.env")
    candidates: List[str] = [home_env]

    if project_root:
        candidates.append(os.path.join(project_root, ".env"))

    cwd_env = os.path.join(os.getcwd(), ".env")
    if cwd_env not in candidates:
        candidates.append(cwd_env)

    for idx, path in enumerate(candidates):
        # 后加载文件默认可以覆盖先加载文件；同时保留调用方 override 的“强覆盖”能力
        file_override = override or (idx > 0)
        loaded = load_env_file(path, override=file_override)
        if loaded:
            loaded_map[path] = sorted(list(loaded.keys()))

    return loaded_map

