"""
Plan 辅助工具：JSON 提取、dot-path 修改、命令模板渲染等。
"""

from __future__ import annotations

import copy
import json
import re
import shutil
from string import Formatter
from typing import Any, Dict, Iterable, Optional


def detect_command_paths(commands: Iterable[str]) -> Dict[str, Optional[str]]:
    """
    探测命令是否可用，返回 cmd -> path/None。
    """

    res: Dict[str, Optional[str]] = {}
    for cmd in commands:
        res[cmd] = shutil.which(cmd)
    return res


def extract_json_object(text: str) -> Dict[str, Any]:
    """
    从 LLM 输出中提取第一个 JSON 对象并解析为 dict。

    设计原因：
    - 即便 prompt 强约束“只输出 JSON”，模型也可能多输出解释/代码块。
    - 这里做一个容错解析，优先寻找最外层 {...}。
    """

    text = text.strip()
    # 直接尝试
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass

    # 尝试从 Markdown 代码块里找
    fence_match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL | re.IGNORECASE)
    if fence_match:
        candidate = fence_match.group(1).strip()
        obj = json.loads(candidate)
        if isinstance(obj, dict):
            return obj

    # 粗暴定位最外层大括号
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("无法在模型输出中找到 JSON 对象")

    candidate = text[start : end + 1]
    obj = json.loads(candidate)
    if not isinstance(obj, dict):
        raise ValueError("解析到的 JSON 不是对象类型")
    return obj


def apply_dotpath_changes(plan: Dict[str, Any], changes: Dict[str, Any]) -> Dict[str, Any]:
    """
    将 FixSuggestion.changes（dot-path -> 值）应用到 plan 上，返回新 dict。

    dot-path 规则：
    - 仅支持 '.' 分隔的字典键路径，例如 "monitor.collectors"
    - 不支持数组索引（保持简单，后续需要再扩展）
    """

    new_plan = copy.deepcopy(plan)
    for path, value in changes.items():
        if not path or not isinstance(path, str):
            continue
        keys = [k for k in path.split(".") if k]
        if not keys:
            continue
        cur: Any = new_plan
        for k in keys[:-1]:
            if not isinstance(cur, dict):
                raise ValueError(f"dot-path 途中不是对象，无法应用修改: {path}")
            if k not in cur or not isinstance(cur[k], dict):
                cur[k] = {}
            cur = cur[k]
        last = keys[-1]
        if not isinstance(cur, dict):
            raise ValueError(f"dot-path 末尾父级不是对象，无法应用修改: {path}")
        cur[last] = value
    return new_plan


def render_cmd_template(parts: list[str], mapping: Dict[str, str]) -> list[str]:
    """
    渲染 cmd 模板：对每一段做 str.format(**mapping)。
    同时校验模板中出现的占位符是否都在 mapping 内。
    """

    rendered: list[str] = []
    formatter = Formatter()
    for p in parts:
        fields = [fname for _, fname, _, _ in formatter.parse(p) if fname]
        missing = [f for f in fields if f not in mapping]
        if missing:
            raise ValueError(f"提交命令模板包含未知占位符: {missing}")
        rendered.append(p.format(**mapping))
    return rendered

