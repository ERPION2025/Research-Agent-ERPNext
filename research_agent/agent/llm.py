"""Provider-neutral chat client with tool calling.

One interface, three providers. The agent loop never imports openai or
anthropic directly, so adding a provider means adding a class here and a
row in the Select field on Research Agent Settings.

Message format used everywhere in this app is the OpenAI shape:

    {"role": "user" | "assistant" | "tool" | "system", "content": str,
     "tool_calls": [...], "tool_call_id": str}

The Anthropic adapter translates in and out of that shape so the rest of
the codebase stays provider-neutral.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import frappe
from frappe import _


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMResponse:
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""
    stop_reason: str = ""

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class BaseProvider:
    def __init__(self, api_key: str, model: str, temperature: float = 0.2):
        self.api_key = api_key
        self.model = model
        self.temperature = temperature

    def chat(self, messages, tools=None, system=None, max_tokens=4096) -> LLMResponse:
        raise NotImplementedError


class OpenAIProvider(BaseProvider):
    """Works with GPT-4.1 family, GPT-4o family and the o-series reasoning models."""

    REASONING_PREFIXES = ("o1", "o3", "o4", "gpt-5")

    def _client(self):
        try:
            from openai import OpenAI
        except ImportError:
            frappe.throw(_("Python package 'openai' is not installed. Run: bench pip install openai"))
        return OpenAI(api_key=self.api_key, timeout=120.0)

    @property
    def _is_reasoning(self) -> bool:
        return self.model.startswith(self.REASONING_PREFIXES)

    def chat(self, messages, tools=None, system=None, max_tokens=4096) -> LLMResponse:
        payload_messages = []
        if system:
            # o-series models take a developer role instead of system
            role = "developer" if self._is_reasoning else "system"
            payload_messages.append({"role": role, "content": system})
        payload_messages.extend(messages)

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": payload_messages,
        }
        if self._is_reasoning:
            kwargs["max_completion_tokens"] = max_tokens
        else:
            kwargs["max_tokens"] = max_tokens
            kwargs["temperature"] = self.temperature

        if tools:
            kwargs["tools"] = [{"type": "function", "function": t} for t in tools]
            kwargs["tool_choice"] = "auto"

        resp = self._client().chat.completions.create(**kwargs)
        choice = resp.choices[0]
        calls = []
        for tc in choice.message.tool_calls or []:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {"_raw": tc.function.arguments}
            calls.append(ToolCall(id=tc.id, name=tc.function.name, arguments=args))

        usage = resp.usage
        return LLMResponse(
            content=choice.message.content,
            tool_calls=calls,
            input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(usage, "completion_tokens", 0) or 0,
            model=self.model,
            stop_reason=choice.finish_reason or "",
        )


class AnthropicProvider(BaseProvider):
    """Claude adapter. Translates OpenAI-shaped messages both ways."""

    def _client(self):
        try:
            import anthropic
        except ImportError:
            frappe.throw(_("Python package 'anthropic' is not installed. Run: bench pip install anthropic"))
        return anthropic.Anthropic(api_key=self.api_key, timeout=120.0)

    @staticmethod
    def _to_anthropic(messages: list[dict]) -> list[dict]:
        out: list[dict] = []
        for m in messages:
            role = m.get("role")
            if role == "tool":
                out.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": m.get("tool_call_id"),
                                "content": str(m.get("content") or ""),
                            }
                        ],
                    }
                )
            elif role == "assistant" and m.get("tool_calls"):
                blocks: list[dict] = []
                if m.get("content"):
                    blocks.append({"type": "text", "text": m["content"]})
                for tc in m["tool_calls"]:
                    fn = tc["function"]
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": tc["id"],
                            "name": fn["name"],
                            "input": json.loads(fn.get("arguments") or "{}"),
                        }
                    )
                out.append({"role": "assistant", "content": blocks})
            else:
                out.append({"role": role, "content": m.get("content") or ""})
        return out

    def chat(self, messages, tools=None, system=None, max_tokens=4096) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": self.temperature,
            "messages": self._to_anthropic(messages),
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = [
                {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "input_schema": t.get("parameters") or {"type": "object", "properties": {}},
                }
                for t in tools
            ]

        resp = self._client().messages.create(**kwargs)
        text_parts, calls = [], []
        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                calls.append(ToolCall(id=block.id, name=block.name, arguments=dict(block.input or {})))

        return LLMResponse(
            content="\n".join(text_parts) or None,
            tool_calls=calls,
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
            model=self.model,
            stop_reason=resp.stop_reason or "",
        )


PROVIDERS = {"OpenAI": OpenAIProvider, "Anthropic": AnthropicProvider}


def get_llm(role: str = "worker", provider: str | None = None, temperature: float = 0.2) -> BaseProvider:
    """Return a configured provider for a role.

    Roles map to the three model slots on Research Agent Settings:
      planner  - decides the plan, usually a reasoning model
      worker   - runs the tool loop, usually a fast cheap model
      reflector- critiques the draft, usually the strongest model available
    """
    settings = frappe.get_cached_doc("Research Agent Settings")
    if not settings.enabled:
        frappe.throw(_("Research Agent is disabled. Enable it in Research Agent Settings."))

    provider = provider or settings.default_provider or "OpenAI"

    if provider == "OpenAI":
        key = settings.get_password("openai_api_key", raise_exception=False)
        model = {
            "planner": settings.openai_planner_model,
            "worker": settings.openai_worker_model,
            "reflector": settings.openai_reflector_model,
        }.get(role) or settings.openai_worker_model or "gpt-4.1-mini"
    elif provider == "Anthropic":
        key = settings.get_password("anthropic_api_key", raise_exception=False)
        model = {
            "planner": settings.anthropic_planner_model,
            "worker": settings.anthropic_worker_model,
            "reflector": settings.anthropic_reflector_model,
        }.get(role) or settings.anthropic_worker_model or "claude-haiku-4-5-20251001"
    else:
        frappe.throw(_("Unknown LLM provider: {0}").format(provider))

    if not key:
        frappe.throw(_("No API key saved for {0}. Add it in Research Agent Settings.").format(provider))

    return PROVIDERS[provider](api_key=key, model=model, temperature=temperature)
