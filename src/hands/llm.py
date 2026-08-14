"""The provider seam: one thin client for any OpenAI-compatible endpoint.

The system deliberately does not care which vendor serves the model — the
planner speaks to this interface, and provider/model/base URL are config. The
default points at a Groq-hosted open model; swapping providers is one config
change, which is the honest answer to "why this provider?".
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openai import APIError, APIStatusError, OpenAI

DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_MODEL = "openai/gpt-oss-120b"
FALLBACK_MODEL = "llama-3.3-70b-versatile"

Message = dict[str, Any]
ToolSpec = dict[str, Any]


class LlmError(Exception):
    """The provider could not produce a usable response."""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    parse_error: str | None = None


@dataclass
class LlmTurn:
    assistant_message: Message
    tool_calls: list[ToolCall]
    prompt_tokens: int
    completion_tokens: int


@dataclass
class LlmClient:
    api_key: str
    model: str = DEFAULT_MODEL
    base_url: str = DEFAULT_BASE_URL
    max_retries: int = 3
    _client: OpenAI = field(init=False)

    def __post_init__(self) -> None:
        self._client = OpenAI(api_key=self.api_key, base_url=self.base_url, max_retries=0)

    def complete(self, messages: list[Message], tools: list[ToolSpec]) -> LlmTurn:
        """One model turn, tool use required. Retries transient provider
        errors (rate limits, 5xx) with backoff; anything else raises."""
        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                response = self._client.chat.completions.create(  # type: ignore[call-overload]
                    model=self.model,
                    messages=messages,
                    tools=tools,
                    tool_choice="required",
                    temperature=0.0,
                )
            except APIStatusError as exc:
                last_error = exc
                if exc.status_code in (429, 500, 502, 503):
                    time.sleep(2.0 * (attempt + 1))
                    continue
                raise LlmError(f"provider error {exc.status_code}: {exc.message}") from exc
            except APIError as exc:
                last_error = exc
                time.sleep(2.0 * (attempt + 1))
                continue
            choice = response.choices[0]
            calls: list[ToolCall] = []
            for tc in choice.message.tool_calls or []:
                if tc.type != "function":  # pragma: no cover - provider-specific
                    continue
                try:
                    arguments = json.loads(tc.function.arguments or "{}")
                    error = None
                except json.JSONDecodeError as exc:
                    arguments = {}
                    error = f"arguments were not valid JSON: {exc}"
                calls.append(
                    ToolCall(
                        id=tc.id,
                        name=tc.function.name,
                        arguments=arguments if isinstance(arguments, dict) else {},
                        parse_error=error,
                    )
                )
            usage = response.usage
            return LlmTurn(
                assistant_message=choice.message.model_dump(exclude_none=True),
                tool_calls=calls,
                prompt_tokens=usage.prompt_tokens if usage else 0,
                completion_tokens=usage.completion_tokens if usage else 0,
            )
        raise LlmError(f"provider unavailable after {self.max_retries} attempts: {last_error}")


def load_dotenv(path: Path) -> None:
    """Minimal .env loader (no dependency): KEY=VALUE lines into os.environ,
    never overriding variables already set."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def client_from_env(model: str | None = None) -> LlmClient:
    load_dotenv(Path(".env"))
    api_key = os.environ.get("GROQ_API_KEY", "")
    if not api_key:
        raise LlmError("GROQ_API_KEY is not set (put it in .env or the environment)")
    return LlmClient(api_key=api_key, model=model or os.environ.get("HANDS_MODEL", DEFAULT_MODEL))
