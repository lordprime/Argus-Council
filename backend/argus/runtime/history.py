"""Keep an agent's message history within a size budget.

An agent's ``messages`` list keeps every tool output (often 40-100k characters each) for up to
``max_agent_iterations`` turns, so each request re-sends all of them: cost grows quadratically and
a long tool loop eventually fails with a context-length error. Before each turn the oldest tool
results are cut to a short preview once the history exceeds the budget. The newest results stay
intact, and every message stays in place, so tool calls and their results remain paired as
providers require.
"""

from __future__ import annotations

from argus.providers.types import Message

# ~30-40k tokens of history, well inside every supported model's context window.
HISTORY_BUDGET_CHARS = 120_000
KEEP_RECENT_TOOL_RESULTS = 3
PREVIEW_CHARS = 800
_MARK = "characters of earlier tool output omitted to save context"


def _chars(messages: list[Message]) -> int:
    return sum(len(m.content) for m in messages)


def compact_history(
    messages: list[Message],
    *,
    budget_chars: int = HISTORY_BUDGET_CHARS,
    keep_recent: int = KEEP_RECENT_TOOL_RESULTS,
) -> int:
    """Shrink older tool results in place, oldest first, until under budget. Returns chars saved."""
    total = _chars(messages)
    if total <= budget_chars:
        return 0
    tool_idx = [i for i, m in enumerate(messages) if m.role == "tool"]
    candidates = tool_idx[: max(len(tool_idx) - keep_recent, 0)]
    saved = 0
    for i in candidates:
        if total - saved <= budget_chars:
            break
        m = messages[i]
        if _MARK in m.content or len(m.content) <= PREVIEW_CHARS * 2:
            continue
        dropped = len(m.content) - PREVIEW_CHARS
        short = f"{m.content[:PREVIEW_CHARS]}\n…[{dropped} {_MARK}]"
        saved += len(m.content) - len(short)
        messages[i] = m.model_copy(update={"content": short})
    return saved
