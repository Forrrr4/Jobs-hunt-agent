"""
统一的 Claude API 调用封装。

所有模块通过本模块调用 LLM，统一处理：
- 模型选择
- 超时与重试（指数退避）
- 错误日志
- 响应解析（纯文本 / JSON）
"""

import json
import logging
import os
from typing import Any, Optional

import anthropic
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
    before_sleep_log,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 默认配置
# ---------------------------------------------------------------------------

DEFAULT_MODEL = "claude-sonnet-4-20250514"
DEFAULT_MAX_TOKENS = 2048
DEFAULT_TEMPERATURE = 0.3
MAX_RETRIES = 3


# ---------------------------------------------------------------------------
# 客户端单例
# ---------------------------------------------------------------------------

_client: Optional[anthropic.AsyncAnthropic] = None


def get_client() -> anthropic.AsyncAnthropic:
    """返回全局 Anthropic 异步客户端（懒加载）。"""
    global _client
    if _client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise EnvironmentError(
                "未找到 ANTHROPIC_API_KEY 环境变量，请先设置：export ANTHROPIC_API_KEY=sk-..."
            )
        _client = anthropic.AsyncAnthropic(api_key=api_key)
    return _client


# ---------------------------------------------------------------------------
# 核心调用函数
# ---------------------------------------------------------------------------

@retry(
    retry=retry_if_exception_type((anthropic.RateLimitError, anthropic.APIConnectionError)),
    stop=stop_after_attempt(MAX_RETRIES),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
async def call_llm(
    prompt: str,
    *,
    system: Optional[str] = None,
    model: str = DEFAULT_MODEL,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    temperature: float = DEFAULT_TEMPERATURE,
) -> str:
    """
    调用 Claude，返回文本响应。

    Args:
        prompt:      用户消息内容
        system:      系统提示词（可选）
        model:       模型名称
        max_tokens:  最大输出 token 数
        temperature: 温度参数

    Returns:
        模型返回的文本字符串

    Raises:
        anthropic.APIError: 非可重试错误（如认证失败、内容审核触发）
    """
    client = get_client()

    messages = [{"role": "user", "content": prompt}]
    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": messages,
    }
    if system:
        kwargs["system"] = system

    logger.debug("调用 LLM | model=%s | prompt_len=%d", model, len(prompt))

    response = await client.messages.create(**kwargs)
    text = response.content[0].text
    logger.debug("LLM 响应 | tokens=%d | len=%d", response.usage.output_tokens, len(text))
    return text


async def call_llm_json(
    prompt: str,
    *,
    system: Optional[str] = None,
    model: str = DEFAULT_MODEL,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    temperature: float = DEFAULT_TEMPERATURE,
    fallback: Optional[dict] = None,
) -> dict:
    """
    调用 Claude 并将响应解析为 JSON dict。

    当模型输出包含 markdown 代码块时自动剥离 ```json ... ```。
    解析失败时返回 fallback（若未提供则重新抛出异常）。

    Returns:
        解析后的 dict

    Raises:
        json.JSONDecodeError: 解析失败且未提供 fallback
    """
    raw = await call_llm(
        prompt,
        system=system,
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
    )

    text = raw.strip()

    # 剥离 markdown 代码块
    if text.startswith("```"):
        lines = text.splitlines()
        # 去掉首行 (```json 或 ```) 和末行 (```)
        inner = lines[1:] if lines[-1].strip() == "```" else lines[1:]
        if inner and inner[-1].strip() == "```":
            inner = inner[:-1]
        text = "\n".join(inner).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        logger.error("JSON 解析失败 | raw=%r | error=%s", raw[:200], exc)
        if fallback is not None:
            return fallback
        raise


# ---------------------------------------------------------------------------
# 便捷封装：轻量级 haiku 调用（用于批量初步筛选）
# ---------------------------------------------------------------------------

async def call_llm_fast(
    prompt: str,
    *,
    system: Optional[str] = None,
    max_tokens: int = 1024,
    temperature: float = 0.1,
) -> str:
    """使用 claude-haiku 模型快速调用，成本更低，适合批量初步筛选。"""
    return await call_llm(
        prompt,
        system=system,
        model="claude-haiku-4-5-20251001",
        max_tokens=max_tokens,
        temperature=temperature,
    )


async def call_llm_fast_json(
    prompt: str,
    *,
    system: Optional[str] = None,
    max_tokens: int = 1024,
    temperature: float = 0.1,
    fallback: Optional[dict] = None,
) -> dict:
    """使用 claude-haiku 模型快速调用并解析 JSON。"""
    return await call_llm_json(
        prompt,
        system=system,
        model="claude-haiku-4-5-20251001",
        max_tokens=max_tokens,
        temperature=temperature,
        fallback=fallback,
    )
