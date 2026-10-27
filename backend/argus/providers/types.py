"""Provider-neutral request/response types."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Role = Literal["system", "user", "assistant", "tool"]
FinishReason = Literal["stop", "length", "tool_calls", "refusal", "error"]
Effort = Literal["low", "medium", "high", "xhigh", "max"]


class ToolSpec(BaseModel):
    name: str = Field(pattern=r"^[a-zA-Z0-9_\-]{1,64}$")
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class Message(BaseModel):
    role: Role
    content: str = ""
    name: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None
    is_error: bool = False
    # Provider-native content blocks for lossless replay to the same provider
    # (e.g. Anthropic thinking blocks must be passed back unchanged).
    raw: dict[str, Any] | None = None

    @classmethod
    def system(cls, text: str) -> Message:
        return cls(role="system", content=text)

    @classmethod
    def user(cls, text: str) -> Message:
        return cls(role="user", content=text)

    @classmethod
    def assistant(cls, text: str, tool_calls: list[ToolCall] | None = None) -> Message:
        return cls(role="assistant", content=text, tool_calls=tool_calls or [])

    @classmethod
    def tool_result(cls, call_id: str, content: str, is_error: bool = False) -> Message:
        return cls(role="tool", content=content, tool_call_id=call_id, is_error=is_error)


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            cost_usd=round(self.cost_usd + other.cost_usd, 8),
        )

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class CompletionRequest(BaseModel):
    model: str
    messages: list[Message]
    tools: list[ToolSpec] = Field(default_factory=list)
    max_tokens: int = Field(default=16_000, ge=1, le=200_000)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    effort: Effort | None = None
    stop: list[str] = Field(default_factory=list)
    json_mode: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def system_prompt(self) -> str:
        return "\n\n".join(m.content for m in self.messages if m.role == "system" and m.content)

    @property
    def conversation(self) -> list[Message]:
        return [m for m in self.messages if m.role != "system"]


class CompletionResponse(BaseModel):
    provider: str
    model: str
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    finish_reason: FinishReason = "stop"
    usage: Usage = Field(default_factory=Usage)
    thinking: str = ""
    raw: dict[str, Any] | None = None
    cached: bool = False
    # "subscription": a CLI agent signed in with the user's vendor account. Such turns carry no API
    # dollar cost (usage.cost_usd == 0); estimated_cost_usd says what they would cost on the API.
    billing: Literal["api", "api_key", "subscription"] = "api"
    estimated_cost_usd: float | None = None
    session_id: str | None = None  # CLI agents: the vendor session this turn ran in

    def as_message(self) -> Message:
        return Message(role="assistant", content=self.content, tool_calls=self.tool_calls, raw=self.raw)


StreamEventType = Literal["text", "thinking", "tool_call", "done", "error"]


class StreamEvent(BaseModel):
    type: StreamEventType
    text: str = ""
    tool_call: ToolCall | None = None
    response: CompletionResponse | None = None
    error: str | None = None
