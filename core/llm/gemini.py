"""Gemini behind the same ``messages.create`` interface the extraction agent uses.

The agent speaks Anthropic-style messages and tool use. This adapter translates:
- messages: text, image (base64) and tool_result blocks become Gemini parts.
- tools: input_schema becomes a function declaration with a JSON schema.
- tool_choice {"type": "tool", "name": X} becomes function calling mode ANY restricted to X.
- responses: function_call parts become tool_use blocks. Their raw Gemini part is kept so it can be
  sent back unchanged on the next turn, which the thought-signature handling needs.
"""

from __future__ import annotations

import base64
import time
from types import SimpleNamespace as NS
from typing import Any, Dict, List, Optional

from google import genai
from google.genai import errors as genai_errors
from google.genai import types

RETRYABLE = {408, 429, 500, 502, 503, 504}
BACKOFF_SECONDS = (5, 15, 30)
REQUEST_TIMEOUT_MS = 180_000


def _tool_names_by_id(messages: List[dict]) -> Dict[str, str]:
    names: Dict[str, str] = {}
    for m in messages:
        if m.get("role") == "assistant" and isinstance(m.get("content"), list):
            for b in m["content"]:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    names[b["id"]] = b["name"]
    return names


class GeminiMessages:
    def __init__(self, client: genai.Client):
        self._client = client

    def create(self, *, model: str, messages: List[dict], max_tokens: int = 4096,
               system: Optional[str] = None, tools: Optional[List[dict]] = None,
               tool_choice: Optional[dict] = None, **_ignored) -> NS:
        config = self._config(max_tokens, system, tools, tool_choice)
        names = _tool_names_by_id(messages)
        contents = [self._content(m, names) for m in messages]
        resp = self._generate(model, contents, config)
        return self._to_message(resp)

    # ------------------------------------------------------------ requests

    @staticmethod
    def _config(max_tokens, system, tools, tool_choice) -> types.GenerateContentConfig:
        kwargs: Dict[str, Any] = {"max_output_tokens": max_tokens}
        if system:
            kwargs["system_instruction"] = system
        if tools:
            kwargs["tools"] = [types.Tool(function_declarations=[
                types.FunctionDeclaration(name=t["name"], description=t.get("description", ""),
                                          parameters_json_schema=t["input_schema"]) for t in tools])]
        if tool_choice:
            mode = types.FunctionCallingConfigMode.AUTO
            allowed = None
            if tool_choice.get("type") == "tool":
                mode = types.FunctionCallingConfigMode.ANY
                allowed = [tool_choice["name"]]
            elif tool_choice.get("type") == "any":
                mode = types.FunctionCallingConfigMode.ANY
            kwargs["tool_config"] = types.ToolConfig(function_calling_config=types.FunctionCallingConfig(
                mode=mode, allowed_function_names=allowed))
        return types.GenerateContentConfig(**kwargs)

    @staticmethod
    def _content(message: dict, names: Dict[str, str]) -> types.Content:
        role = "model" if message["role"] == "assistant" else "user"
        content = message["content"]
        if isinstance(content, str):
            return types.Content(role=role, parts=[types.Part(text=content)])
        parts: List[types.Part] = []
        for b in content:
            kind = b.get("type")
            if kind == "text":
                parts.append(types.Part(text=b["text"]))
            elif kind == "image":
                src = b["source"]
                parts.append(types.Part(inline_data=types.Blob(
                    mime_type=src["media_type"], data=base64.b64decode(src["data"]))))
            elif kind == "tool_use":
                raw = b.get("_raw")
                parts.append(raw if isinstance(raw, types.Part) else types.Part(
                    function_call=types.FunctionCall(name=b["name"], args=b["input"])))
            elif kind == "tool_result":
                parts.append(types.Part(function_response=types.FunctionResponse(
                    name=names[b["tool_use_id"]],
                    response={"result": b.get("content", ""), "is_error": bool(b.get("is_error"))})))
            else:
                raise ValueError(f"unsupported block type for Gemini: {kind!r}")
        return types.Content(role=role, parts=parts)

    def _generate(self, model, contents, config):
        """Retries rate limits, server errors and timeouts with backoff. Other errors are raised at once."""
        for attempt in range(len(BACKOFF_SECONDS) + 1):
            try:
                return self._client.models.generate_content(model=model, contents=contents, config=config)
            except genai_errors.APIError as e:
                retry = getattr(e, "code", None) in RETRYABLE
            except (TimeoutError, ConnectionError) as e:
                retry = True
            if retry and attempt < len(BACKOFF_SECONDS):
                time.sleep(BACKOFF_SECONDS[attempt])
                continue
            raise RuntimeError(f"model call failed after {attempt + 1} attempt(s); try again later") from None

    # ----------------------------------------------------------- responses

    @staticmethod
    def _to_message(resp) -> NS:
        cand = resp.candidates[0] if resp.candidates else None
        parts = (cand.content.parts if cand and cand.content and cand.content.parts else [])
        blocks: List[NS] = []
        n = 0
        for p in parts:
            if p.function_call is not None:
                n += 1
                fc = p.function_call
                blocks.append(NS(type="tool_use", name=fc.name, input=dict(fc.args or {}),
                                 id=fc.id or f"gemini-call-{n}", _raw=p))
            elif p.text and not p.thought:
                blocks.append(NS(type="text", text=p.text))
        stop = "tool_use" if any(b.type == "tool_use" for b in blocks) else "end_turn"
        return NS(content=blocks, stop_reason=stop)


class GeminiClient:
    """Duck-typed stand-in for anthropic.Anthropic: exposes .messages.create."""

    def __init__(self, api_key: str):
        self.messages = GeminiMessages(genai.Client(
            api_key=api_key, http_options=types.HttpOptions(timeout=REQUEST_TIMEOUT_MS)))
