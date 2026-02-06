"""
LLM 工厂：创建 OpenAI-compatible 的 DeepSeek 模型实例。

说明：
- 这里选择“OpenAI-compatible”方式接入，便于未来替换其它兼容服务。
- 仅在启用 agent 模式时才会 import LangChain 相关依赖，避免影响基础功能。
"""

import inspect
import os
from dataclasses import dataclass
from typing import Dict, Optional


@dataclass(frozen=True)
class DeepSeekConfig:
    """
    DeepSeek（OpenAI-compatible）连接配置。
    """

    api_key: str
    base_url: str
    model: str = "deepseek-chat"
    temperature: float = 0.0


def _pick_env(*keys: str) -> Optional[str]:
    for k in keys:
        v = os.getenv(k)
        if v:
            return v
    return None


def load_deepseek_config_from_env(
    *,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    model: Optional[str] = None,
    temperature: Optional[float] = None,
) -> DeepSeekConfig:
    """
    从参数/环境变量加载 DeepSeek 配置。

    环境变量优先级（从高到低）：
    - API Key: DEEPSEEK_API_KEY, OPENAI_API_KEY
    - Base URL: DEEPSEEK_BASE_URL, OPENAI_BASE_URL, OPENAI_API_BASE
    - Model: DEEPSEEK_MODEL
    """

    resolved_api_key = api_key or _pick_env("DEEPSEEK_API_KEY", "OPENAI_API_KEY")
    resolved_base_url = base_url or _pick_env("DEEPSEEK_BASE_URL", "OPENAI_BASE_URL", "OPENAI_API_BASE")
    resolved_model = model or _pick_env("DEEPSEEK_MODEL") or "deepseek-chat"
    resolved_temperature = 0.0 if temperature is None else float(temperature)

    if not resolved_api_key:
        raise RuntimeError("缺少 API Key：请设置环境变量 DEEPSEEK_API_KEY（或 OPENAI_API_KEY）")
    if not resolved_base_url:
        raise RuntimeError("缺少 Base URL：请设置环境变量 DEEPSEEK_BASE_URL（或 OPENAI_BASE_URL/OPENAI_API_BASE）")

    return DeepSeekConfig(
        api_key=resolved_api_key,
        base_url=resolved_base_url,
        model=resolved_model,
        temperature=resolved_temperature,
    )


def create_deepseek_chat_llm(cfg: DeepSeekConfig):
    """
    创建 LangChain 的 ChatOpenAI 实例（用于对接 DeepSeek OpenAI-compatible 服务）。
    """

    try:
        from langchain_openai import ChatOpenAI
    except Exception as e:  # pragma: no cover
        raise RuntimeError(
            "未安装依赖：请安装 langchain-openai（以及其依赖 openai）。例如：pip install langchain-openai"
        ) from e

    # 兼容不同版本的 langchain_openai 参数命名（base_url / openai_api_base，api_key / openai_api_key）
    kwargs: Dict[str, object] = {
        "model": cfg.model,
        "temperature": cfg.temperature,
    }

    sig = inspect.signature(ChatOpenAI.__init__)
    if "api_key" in sig.parameters:
        kwargs["api_key"] = cfg.api_key
    elif "openai_api_key" in sig.parameters:
        kwargs["openai_api_key"] = cfg.api_key

    if "base_url" in sig.parameters:
        kwargs["base_url"] = cfg.base_url
    elif "openai_api_base" in sig.parameters:
        kwargs["openai_api_base"] = cfg.base_url

    return ChatOpenAI(**kwargs)
