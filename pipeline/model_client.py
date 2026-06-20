"""Unified LLM client supporting DeepSeek, Qwen, and OpenAI providers.

Uses httpx to call OpenAI-compatible APIs directly, with retry, token
estimation, and cost calculation built in.

Environment variables:
    LLM_PROVIDER: One of ``deepseek`` (default), ``qwen``, ``openai``.
    DEEPSEEK_API_KEY / DASHSCOPE_API_KEY / OPENAI_API_KEY: API keys.
    LLM_MODEL: Override the default model for the selected provider.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class Usage:
    """Token usage statistics returned by the LLM API.

    Attributes:
        prompt_tokens: Number of tokens in the prompt.
        completion_tokens: Number of tokens in the generated response.
        total_tokens: Sum of prompt and completion tokens.
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass
class LLMResponse:
    """Unified response from an LLM call.

    Attributes:
        content: The generated text content.
        model: The model that produced this response.
        usage: Token usage details.
        finish_reason: Reason the generation stopped (stop, length, etc.).
        latency_ms: Round-trip latency in milliseconds.
    """

    content: str
    model: str = ""
    usage: Usage = field(default_factory=Usage)
    finish_reason: str = ""
    latency_ms: float = 0.0


# ---------------------------------------------------------------------------
# Provider configuration
# ---------------------------------------------------------------------------

# Pricing in USD per 1M tokens (approximate, may vary by specific model)
_PROVIDER_CONFIGS: dict[str, dict[str, Any]] = {
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "default_model": "deepseek-chat",
        "key_env": "DEEPSEEK_API_KEY",
        "pricing_input": 0.27,
        "pricing_output": 1.10,
    },
    "qwen": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "default_model": "qwen-plus",
        "key_env": "DASHSCOPE_API_KEY",
        "pricing_input": 0.80,
        "pricing_output": 2.00,
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4o-mini",
        "key_env": "OPENAI_API_KEY",
        "pricing_input": 0.15,
        "pricing_output": 0.60,
    },
}

_DEFAULT_PROVIDER = "deepseek"
_DEFAULT_TIMEOUT = 60.0
_MAX_RETRIES = 3
_RETRY_BASE_DELAY = 1.0


def _resolve_provider() -> str:
    """Read ``LLM_PROVIDER`` from env, falling back to deepseek.

    Returns:
        Normalized provider name string.
    """
    raw = os.environ.get("LLM_PROVIDER", _DEFAULT_PROVIDER).strip().lower()
    if raw not in _PROVIDER_CONFIGS:
        logger.warning(
            "未知的 LLM_PROVIDER=%s, 回退到 %s", raw, _DEFAULT_PROVIDER
        )
        raw = _DEFAULT_PROVIDER
    return raw


def _get_api_key(provider: str) -> Optional[str]:
    """Look up the API key for *provider* from environment variables.

    Args:
        provider: Provider name (``deepseek`` / ``qwen`` / ``openai``).

    Returns:
        The API key string, or ``None`` if not set.
    """
    key_env = _PROVIDER_CONFIGS[provider]["key_env"]
    return os.environ.get(key_env)


# ---------------------------------------------------------------------------
# Abstract provider
# ---------------------------------------------------------------------------


class LLMProvider(ABC):
    """Abstract interface for an LLM provider."""

    @abstractmethod
    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        model: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> LLMResponse:
        """Send a chat completion request.

        Args:
            messages: List of message dicts with ``role`` and ``content`` keys.
            model: Model name override (uses provider default if ``None``).
            temperature: Sampling temperature (0.0-2.0).
            max_tokens: Maximum tokens in the response.
            timeout: Request timeout in seconds.

        Returns:
            An :class:`LLMResponse` with the generated content.
        """
        ...


# ---------------------------------------------------------------------------
# OpenAI-compatible HTTP implementation
# ---------------------------------------------------------------------------


class OpenAICompatibleProvider(LLMProvider):
    """Generic provider for any OpenAI-compatible chat completions endpoint.

    Args:
        base_url: Base URL for the API (e.g. ``https://api.openai.com/v1``).
        api_key: API key string.
        default_model: Model name used when none is specified per-request.
        pricing_input: USD cost per 1M input tokens.
        pricing_output: USD cost per 1M output tokens.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        default_model: str,
        pricing_input: float = 0.0,
        pricing_output: float = 0.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.default_model = default_model
        self.pricing_input = pricing_input
        self.pricing_output = pricing_output
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                timeout=httpx.Timeout(_DEFAULT_TIMEOUT),
            )
        return self._client

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        model: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> LLMResponse:
        """Send a chat completion request to the OpenAI-compatible endpoint.

        Args:
            messages: List of message dicts with ``role`` and ``content`` keys.
            model: Model override (uses ``self.default_model`` if ``None``).
            temperature: Sampling temperature.
            max_tokens: Maximum response tokens.
            timeout: Per-request timeout in seconds.

        Returns:
            An :class:`LLMResponse`.

        Raises:
            httpx.HTTPError: On transport or HTTP-level failures.
            ValueError: If the response body is missing expected fields.
        """
        resolved_model = model or self.default_model
        payload: dict[str, Any] = {
            "model": resolved_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        client = await self._get_client()
        start = time.monotonic()

        resp = await client.post(
            "/chat/completions",
            json=payload,
            timeout=httpx.Timeout(timeout),
        )
        resp.raise_for_status()
        body = resp.json()

        elapsed_ms = (time.monotonic() - start) * 1000

        choice = body.get("choices", [{}])[0]
        usage_raw = body.get("usage", {})

        return LLMResponse(
            content=choice.get("message", {}).get("content", ""),
            model=body.get("model", resolved_model),
            usage=Usage(
                prompt_tokens=usage_raw.get("prompt_tokens", 0),
                completion_tokens=usage_raw.get("completion_tokens", 0),
                total_tokens=usage_raw.get("total_tokens", 0),
            ),
            finish_reason=choice.get("finish_reason", ""),
            latency_ms=round(elapsed_ms, 1),
        )

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    def estimate_tokens(self, text: str) -> int:
        """Rough token-count estimation based on character ratios.

        Chinese / CJK characters count as ~0.5 tokens each; ASCII / Latin
        characters count as ~0.25 tokens each (≈ 4 chars / token).

        Args:
            text: Input text to estimate.

        Returns:
            Estimated token count (never less than 1).
        """
        cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff" or "\u3000" <= ch <= "\u303f")
        ascii_chars = len(text) - cjk
        estimated = int(cjk * 0.5 + ascii_chars * 0.25) or 1
        return estimated

    def estimate_cost(
        self, prompt_text: str, response_text: Optional[str] = None
    ) -> float:
        """Estimate USD cost for a prompt and optional response.

        Args:
            prompt_text: The full prompt text.
            response_text: The LLM response text (optional).

        Returns:
            Estimated cost in USD.
        """
        input_tokens = self.estimate_tokens(prompt_text)
        output_tokens = self.estimate_tokens(response_text) if response_text else 0
        input_cost = (input_tokens / 1_000_000) * self.pricing_input
        output_cost = (output_tokens / 1_000_000) * self.pricing_output
        return round(input_cost + output_cost, 6)

    def estimate_cost_from_usage(
        self, usage: Usage
    ) -> float:
        """Calculate cost from actual API usage data.

        Args:
            usage: The :class:`Usage` returned by the API.

        Returns:
            Cost in USD.
        """
        input_cost = (usage.prompt_tokens / 1_000_000) * self.pricing_input
        output_cost = (usage.completion_tokens / 1_000_000) * self.pricing_output
        return round(input_cost + output_cost, 6)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


_provider_instance: Optional[OpenAICompatibleProvider] = None


def get_provider() -> OpenAICompatibleProvider:
    """Create or return the cached provider singleton.

    The provider is selected via ``LLM_PROVIDER`` env var. The corresponding
    API key env var must also be set.

    Returns:
        A configured :class:`OpenAICompatibleProvider`.

    Raises:
        RuntimeError: If the required API key is not set.
    """
    global _provider_instance
    if _provider_instance is not None and (
        _provider_instance._client is None or not _provider_instance._client.is_closed
    ):
        return _provider_instance

    name = _resolve_provider()
    cfg = _PROVIDER_CONFIGS[name]
    api_key = _get_api_key(name)
    if not api_key:
        raise RuntimeError(
            f"缺少 API Key: 请设置环境变量 {cfg['key_env']}"
        )

    _provider_instance = OpenAICompatibleProvider(
        base_url=cfg["base_url"],
        api_key=api_key,
        default_model=os.environ.get("LLM_MODEL", cfg["default_model"]),
        pricing_input=cfg["pricing_input"],
        pricing_output=cfg["pricing_output"],
    )
    logger.info(
        "LLM 客户端已初始化: provider=%s model=%s",
        name,
        _provider_instance.default_model,
    )
    return _provider_instance


def reset_provider() -> None:
    """Reset the cached provider instance (useful for testing)."""
    global _provider_instance
    _provider_instance = None


# ---------------------------------------------------------------------------
# Retry wrapper
# ---------------------------------------------------------------------------


async def chat_with_retry(
    messages: list[dict[str, str]],
    *,
    model: Optional[str] = None,
    temperature: float = 0.7,
    max_tokens: int = 4096,
    timeout: float = _DEFAULT_TIMEOUT,
    max_retries: int = _MAX_RETRIES,
) -> LLMResponse:
    """Call the LLM with automatic retry on transient failures.

    Uses exponential backoff: 1 s, 2 s, 4 s between successive retries.

    Args:
        messages: Chat messages.
        model: Model name override.
        temperature: Sampling temperature.
        max_tokens: Maximum response tokens.
        timeout: Per-request timeout in seconds.
        max_retries: Maximum number of retry attempts (default 3).

    Returns:
        An :class:`LLMResponse`.

    Raises:
        RuntimeError: If all retries are exhausted.
    """
    provider = get_provider()
    last_error: Optional[Exception] = None

    for attempt in range(max_retries + 1):
        try:
            return await provider.chat(
                messages,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=timeout,
            )
        except (httpx.TimeoutException, httpx.HTTPStatusError) as exc:
            last_error = exc
            if attempt < max_retries:
                delay = _RETRY_BASE_DELAY * (2 ** attempt)
                logger.warning(
                    "LLM 请求失败 (attempt %d/%d): %s, %0.1fs 后重试",
                    attempt + 1,
                    max_retries + 1,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)
        except httpx.HTTPError as exc:
            # Non-retryable HTTP errors (e.g. invalid URL)
            raise RuntimeError(f"LLM 请求失败: {exc}") from exc

    raise RuntimeError(
        f"LLM 请求在 {max_retries + 1} 次尝试后仍然失败: {last_error}"
    )


# ---------------------------------------------------------------------------
# Convenience API
# ---------------------------------------------------------------------------


async def quick_chat(
    prompt: str,
    *,
    system: Optional[str] = None,
    model: Optional[str] = None,
    temperature: float = 0.7,
    max_tokens: int = 4096,
) -> str:
    """One-shot LLM call: send a prompt and return the text response.

    Args:
        prompt: The user message content.
        system: Optional system prompt.
        model: Model name override.
        temperature: Sampling temperature.
        max_tokens: Maximum response tokens.

    Returns:
        The generated text content.
    """
    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    response = await chat_with_retry(
        messages,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return response.content


def estimate_text_tokens(text: str) -> int:
    """Estimate token count for a piece of text using the current provider.

    Args:
        text: Input text.

    Returns:
        Estimated token count.
    """
    return get_provider().estimate_tokens(text)


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------


async def _self_test() -> None:
    """Run a quick smoke test when the module is executed directly."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    provider_name = _resolve_provider()
    logger.info("当前 LLM 提供商: %s", provider_name)

    api_key = _get_api_key(provider_name)
    if not api_key:
        logger.warning(
            "未设置 API Key (需要 %s), 跳过实机测试",
            _PROVIDER_CONFIGS[provider_name]["key_env"],
        )
        logger.info("模块加载正常, 数据结构和工厂函数可用")
        return

    try:
        provider = get_provider()
    except RuntimeError as exc:
        logger.error("初始化失败: %s", exc)
        return

    # Token estimation test
    sample = "Hello, this is a test. 这是一个测试。"
    estimated = provider.estimate_tokens(sample)
    logger.info("Token 估算: '%s' → %d tokens", sample, estimated)

    estimated_cost = provider.estimate_cost(sample, "OK")
    logger.info("成本估算: $%.6f", estimated_cost)
    logger.info("定价: input=$%.2f/M output=$%.2f/M",
                provider.pricing_input, provider.pricing_output)

    # Quick chat (requires API key)
    logger.info("发送测试请求 (quick_chat)...")
    try:
        result = await quick_chat(
            "用一句话回答: 什么是 LangGraph?",
            system="你是一个技术助手, 回答简洁。",
            max_tokens=256,
        )
        logger.info("测试回复: %s", result[:100])
    except RuntimeError as exc:
        logger.error("请求失败: %s", exc)
    finally:
        await provider.close()


if __name__ == "__main__":
    asyncio.run(_self_test())
