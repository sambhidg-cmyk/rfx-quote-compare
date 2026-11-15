"""Model client factory. Provider comes from LLM_PROVIDER / EXTRACT_PROVIDER, default gemini.

    GEMINI_API_KEY      required for provider gemini (set it in the environment; never commit it)
    ANTHROPIC_API_KEY   required for provider anthropic
    EXTRACT_MODEL       overrides the extraction model name
"""

from __future__ import annotations

import os
from typing import Optional

DEFAULT_MODELS = {"gemini": "gemini-3.1-pro-preview", "anthropic": "claude-sonnet-5-5"}


def provider_name(provider: Optional[str] = None) -> str:
    p = (provider or os.environ.get("EXTRACT_PROVIDER") or os.environ.get("LLM_PROVIDER") or "gemini").lower()
    if p not in DEFAULT_MODELS:
        raise ValueError(f"unknown provider {p!r}; use one of {sorted(DEFAULT_MODELS)}")
    return p


def default_model(provider: Optional[str] = None) -> str:
    return os.environ.get("EXTRACT_MODEL") or DEFAULT_MODELS[provider_name(provider)]


def make_client(provider: Optional[str] = None):
    p = provider_name(provider)
    if p == "gemini":
        key = os.environ.get("GEMINI_API_KEY")
        if not key:
            raise RuntimeError("GEMINI_API_KEY is not set")
        from core.llm.gemini import GeminiClient

        return GeminiClient(key)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    from anthropic import Anthropic

    return Anthropic()
