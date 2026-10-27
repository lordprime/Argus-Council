"""Catalog of supported provider kinds and how to construct them."""

from __future__ import annotations

from dataclasses import dataclass, field

from argus.providers.anthropic import AnthropicProvider
from argus.providers.base import Provider
from argus.providers.gemini import GeminiProvider
from argus.providers.mock import MockProvider
from argus.providers.openai_compat import OpenAICompatProvider


@dataclass(frozen=True)
class ProviderKind:
    key: str
    label: str
    protocol: str  # anthropic | openai | gemini | mock
    default_base_url: str | None
    requires_api_key: bool
    self_hosted: bool = False
    suggested_models: tuple[str, ...] = field(default_factory=tuple)


KINDS: dict[str, ProviderKind] = {
    k.key: k
    for k in [
        ProviderKind(
            "anthropic",
            "Anthropic Claude",
            "anthropic",
            None,
            True,
            suggested_models=("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"),
        ),
        ProviderKind("openai", "OpenAI", "openai", "https://api.openai.com/v1", True),
        ProviderKind(
            "gemini", "Google Gemini", "gemini", "https://generativelanguage.googleapis.com/v1beta", True
        ),
        ProviderKind("mistral", "Mistral AI", "openai", "https://api.mistral.ai/v1", True),
        ProviderKind("groq", "Groq", "openai", "https://api.groq.com/openai/v1", True),
        ProviderKind("openrouter", "OpenRouter", "openai", "https://openrouter.ai/api/v1", True),
        ProviderKind("deepseek", "DeepSeek", "openai", "https://api.deepseek.com/v1", True),
        ProviderKind("xai", "xAI", "openai", "https://api.x.ai/v1", True),
        ProviderKind("together", "Together AI", "openai", "https://api.together.xyz/v1", True),
        ProviderKind(
            "ollama", "Ollama (self-hosted)", "openai", "http://ollama:11434/v1", False, self_hosted=True
        ),
        ProviderKind("vllm", "vLLM (self-hosted)", "openai", "http://vllm:8000/v1", False, self_hosted=True),
        ProviderKind(
            "lmstudio",
            "LM Studio (self-hosted)",
            "openai",
            "http://host.docker.internal:1234/v1",
            False,
            self_hosted=True,
        ),
        ProviderKind(
            "llamacpp",
            "llama.cpp server (self-hosted)",
            "openai",
            "http://llamacpp:8080/v1",
            False,
            self_hosted=True,
        ),
        ProviderKind(
            "openai_compatible", "Custom OpenAI-compatible endpoint", "openai", None, False, self_hosted=True
        ),
        ProviderKind(
            "mock",
            "Mock (offline, deterministic)",
            "mock",
            None,
            False,
            self_hosted=True,
            suggested_models=("mock-1",),
        ),
    ]
}


def build_provider(
    kind: str,
    *,
    name: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    options: dict | None = None,
) -> Provider:
    spec = KINDS.get(kind)
    if spec is None:
        raise ValueError(f"Unknown provider kind: {kind}")
    options = options or {}
    url = base_url or spec.default_base_url
    if spec.protocol == "anthropic":
        return AnthropicProvider(
            api_key=api_key,
            base_url=base_url,
            name=name or kind,
            refusal_fallback=options.get("refusal_fallback", True),
        )
    if spec.protocol == "gemini":
        return GeminiProvider(api_key, base_url=url or spec.default_base_url or "", name=name or kind)
    if spec.protocol == "openai":
        if not url:
            raise ValueError(f"Provider kind '{kind}' requires base_url")
        headers = {}
        if kind == "openrouter":
            headers = {"X-Title": "Argus Council"}
        return OpenAICompatProvider(
            base_url=url, api_key=api_key, name=name or kind, kind=kind, extra_headers=headers
        )
    return MockProvider(name=name or kind)
