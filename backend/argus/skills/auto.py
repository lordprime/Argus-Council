"""Which skills an agent's task calls for, so they are loaded for it without waiting for the model
to call ``use_skill`` (models often skip it).

A skill's description says when to use it ("Use for any research-mode deliverable…", "Use when
asked to review, audit or critique code…"). A skill is picked when that clause shares enough words
with the run's mode, the agent's role and its task, or names the run's mode outright.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

# Words too common to say anything about a task.
_STOP = frozenset(
    """
    about above after again also always and another any are around asked before being between both
    but can could does doing each either else even every for from further have here how into its
    just like make many more most much must need never not now off once only other our out over own
    same should some such than that the their them then there these they this those through too
    under until use used uses using very via want was well were what when whenever where whether
    which while who whose why will with within without would you your yours task tasks work
    council councils member members agent agents user users run runs step steps topic
    """.split()
)
_WORD = re.compile(r"[a-z][a-z0-9]{2,}")
# Picked when the skill's clause names the run's mode, or the overlap scores at least this (the
# skill's whole name found in the task counts 2).
MIN_SCORE = 3
DEFAULT_LIMIT = 2


def _words(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _STOP}


def _when_clause(description: str) -> str:
    """The "Use when/for/whenever…" part of a description (the whole description if it has none)."""
    match = re.search(r"\buse\b(.*)$", description, re.IGNORECASE | re.DOTALL)
    return match.group(1) if match else description


def pick_skills(
    skills: Mapping[str, Any], *, mode: str, role: str, text: str, limit: int = DEFAULT_LIMIT
) -> list[str]:
    """Names of the skills (at most ``limit``, best first) that the task described by ``mode``,
    ``role`` and ``text`` calls for."""
    context = _words(f"{mode} {role} {text}")
    scored: list[tuple[int, str]] = []
    for name, pkg in skills.items():
        when = _when_clause(str(getattr(pkg, "description", "") or ""))
        name_words = _words(name.replace("-", " "))
        # The skill's whole name in the task (e.g. "code" and "review") counts 2; one word of it
        # ("review" a draft) says nothing.
        named = bool(name_words) and name_words <= _words(text)
        score = len(_words(when) & context) + (2 if named else 0)
        mode_named = bool(mode) and re.search(rf"\b{re.escape(mode.lower())}\b", when.lower())
        if mode_named:
            score += 2
        if mode_named or score >= MIN_SCORE:
            scored.append((score, name))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [name for _, name in scored[:limit]]
