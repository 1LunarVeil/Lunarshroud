"""Conversation transcript helpers for the agent loop.

The transcript is immutable from the loop's point of view: each step produces a new
list. That is not stylistic — it removes a whole class of bug where one turn's
bookkeeping mutates a list another part is still reading (a streamed reply being
rendered, or a retry re-sending a half-updated history).

Values are built through `dataclasses.replace` on a small record, so a new turn is
an explicit new object rather than a dict mutated in place.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Turn:
    """One message in the transcript. Immutable by construction."""

    role: str
    content: str = ""
    reasoning: str = ""
    tool_call_id: str | None = None
    tool_calls: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    def to_message(self) -> dict[str, Any]:
        message: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_call_id:
            message["tool_call_id"] = self.tool_call_id
        if self.tool_calls:
            message["tool_calls"] = list(self.tool_calls)
        return message


def user_turn(content: str) -> Turn:
    """A message the user typed. The loop starts every run with one of these."""
    return Turn(role="user", content=content)


def assistant_turn(content: str, reasoning: str, tool_calls: Any) -> Turn:
    calls = tool_calls if isinstance(tool_calls, list) else []
    return Turn(
        role="assistant",
        content=content,
        reasoning=reasoning,
        tool_calls=tuple(entry for entry in calls if isinstance(entry, dict)),
    )


def tool_turn(call_id: str, content: str) -> Turn:
    return Turn(role="tool", content=content, tool_call_id=call_id)


def extend(transcript: list[Turn], *turns: Turn) -> list[Turn]:
    """Return a new transcript with `turns` appended.

    A tuple-returning helper rather than `list.append`: every caller gets a fresh
    list, so a reader holding the previous value never observes a mid-turn state.
    """
    return list(transcript) + list(turns)


def with_system_prompt(transcript: list[Turn], prompt: str) -> list[dict[str, Any]]:
    """Render the transcript as request messages, folding in a system prompt.

    A conversation may already begin with a system message. Prepending a second one
    confuses chat templates that expect exactly one, so the prompt is merged into
    the existing entry instead.
    """
    messages = [turn.to_message() for turn in transcript]
    text = (prompt or "").strip()
    if not text:
        return messages

    if messages and messages[0].get("role") == "system":
        merged = dict(messages[0])
        existing = str(merged.get("content") or "")
        merged["content"] = text + "\n\n" + existing if existing else text
        return [merged, *messages[1:]]

    return [{"role": "system", "content": text}, *messages]
