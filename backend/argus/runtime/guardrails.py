"""Guardrails: the limits every council run operates inside.

Deterministic checks (budget, steps, runtime, tool allow/deny, approvals,
content policy, sandbox paths) run on every action. Sub-councils inherit
their parent's limits as ceilings and may only tighten them.
"""

from __future__ import annotations

import fnmatch
import re
import time
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from argus.providers.types import Usage

InterceptMode = Literal["annotate", "hold", "block"]
Action = Literal["warned", "blocked", "truncated", "halted"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Budget(_Strict):
    usd: float | None = Field(default=5.0, ge=0)
    tokens: int | None = Field(default=2_000_000, ge=0)


class ToolPolicy(_Strict):
    allow: list[str] = Field(default_factory=lambda: ["*"])
    deny: list[str] = Field(default_factory=list)


class NetworkPolicy(_Strict):
    allow_domains: list[str] = Field(default_factory=lambda: ["*"])
    block_private_ips: bool = True


class ContentPolicy(_Strict):
    preset: Literal["off", "standard", "strict"] = "standard"
    blocked_topics: list[str] = Field(default_factory=list)


class SandboxPolicy(_Strict):
    root: str = "/workspace"
    read_only_paths: list[str] = Field(default_factory=list)
    max_file_mb: float = Field(default=10, gt=0, le=512)


class Oversight(_Strict):
    intercept_mode: InterceptMode = "hold"
    monitor_sensitivity: Literal["off", "low", "medium", "high"] = "medium"
    loop_threshold: int = Field(default=3, ge=2, le=20)
    max_corrections_per_thought: int = Field(default=2, ge=0, le=10)


class Guardrails(_Strict):
    budget: Budget = Field(default_factory=Budget)
    max_steps: int = Field(default=80, ge=1, le=10_000)
    max_runtime_minutes: float = Field(default=60, gt=0, le=24 * 60)
    max_parallel_agents: int = Field(default=4, ge=1, le=64)
    max_agent_iterations: int = Field(default=12, ge=1, le=200)
    max_subcouncil_depth: int = Field(default=2, ge=0, le=5)
    tools: ToolPolicy = Field(default_factory=ToolPolicy)
    skills: ToolPolicy = Field(default_factory=ToolPolicy)
    network: NetworkPolicy = Field(default_factory=NetworkPolicy)
    approval_required: list[str] = Field(
        default_factory=lambda: ["shell", "package_install", "remote_agent", "network_write"]
    )
    content_policy: ContentPolicy = Field(default_factory=ContentPolicy)
    sandbox: SandboxPolicy = Field(default_factory=SandboxPolicy)
    oversight: Oversight = Field(default_factory=Oversight)

    @classmethod
    def resolve(cls, *layers: dict[str, Any] | None) -> Guardrails:
        """Deep-merge layers (defaults < council < run); later layers override earlier ones."""
        merged: dict[str, Any] = {}
        for layer in layers:
            if layer:
                merged = _deep_merge(merged, layer)
        return cls.model_validate(merged)

    def clamp_to(self, parent: Guardrails) -> Guardrails:
        """Return a copy that never exceeds ``parent``'s ceilings (used for sub-councils)."""
        data = self.model_dump()

        def lo(a: float | None, b: float | None) -> float | None:
            if a is None:
                return b
            if b is None:
                return a
            return min(a, b)

        data["budget"]["usd"] = lo(self.budget.usd, parent.budget.usd)
        data["budget"]["tokens"] = lo(self.budget.tokens, parent.budget.tokens)
        for key in ("max_steps", "max_runtime_minutes", "max_parallel_agents", "max_agent_iterations"):
            data[key] = min(getattr(self, key), getattr(parent, key))
        data["max_subcouncil_depth"] = max(min(self.max_subcouncil_depth, parent.max_subcouncil_depth - 1), 0)
        data["tools"]["deny"] = sorted(set(self.tools.deny) | set(parent.tools.deny))
        if parent.tools.allow != ["*"]:
            data["tools"]["allow"] = [t for t in self.tools.allow if _matches(t, parent.tools.allow)] or list(
                parent.tools.allow
            )
        data["skills"]["deny"] = sorted(set(self.skills.deny) | set(parent.skills.deny))
        data["approval_required"] = sorted(set(self.approval_required) | set(parent.approval_required))
        return Guardrails.model_validate(data)


def _deep_merge(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    out = dict(a)
    for k, v in b.items():
        out[k] = _deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _matches(name: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(name, p) for p in patterns)


def _minus(a: Usage, b: Usage) -> Usage:
    return Usage(
        input_tokens=max(a.input_tokens - b.input_tokens, 0),
        output_tokens=max(a.output_tokens - b.output_tokens, 0),
        cache_read_tokens=max(a.cache_read_tokens - b.cache_read_tokens, 0),
        cache_write_tokens=max(a.cache_write_tokens - b.cache_write_tokens, 0),
        cost_usd=max(round(a.cost_usd - b.cost_usd, 8), 0.0),
    )


@dataclass
class Violation:
    rule: str
    limit: Any
    observed: Any
    action: Action
    message: str
    # Where it was seen, e.g. the text around a flagged phrase (shown when the event is expanded).
    excerpt: str = ""

    def as_event(self, target: dict[str, Any] | None = None) -> dict[str, Any]:
        event = {
            "rule": self.rule,
            "limit": self.limit,
            "observed": self.observed,
            "action": self.action,
            "message": self.message,
            "target": target or {},
        }
        if self.excerpt:
            event["excerpt"] = self.excerpt
        return event


class GuardrailBreach(Exception):
    def __init__(self, violation: Violation) -> None:
        super().__init__(violation.message)
        self.violation = violation


# Patterns for content screening. Deliberately conservative: they flag, the
# oversight layer decides.
_SECRET_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"sk-[A-Za-z0-9]{32,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"ghp_[A-Za-z0-9]{36}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"argus_[A-Za-z0-9_\-]{40,}"),
]
_INJECTION_PATTERNS = [
    re.compile(r"ignore (?:all |any )?(?:previous|prior|above) (?:instructions|directions)", re.I),
    re.compile(r"disregard (?:your|the) (?:system prompt|instructions|guardrails)", re.I),
    re.compile(r"you are now (?:in )?(?:developer|dan|jailbreak) mode", re.I),
    re.compile(r"reveal (?:your|the) (?:system prompt|api key|secrets?)", re.I),
]
_DANGEROUS_SHELL = [
    re.compile(r"\brm\s+-[a-z]*r[a-z]*f?\s+(/|~|\$HOME)(\s|$)"),
    re.compile(r":\(\)\s*\{\s*:\|:&\s*\};:"),
    re.compile(r"\bmkfs(\.\w+)?\b"),
    re.compile(r"\bdd\s+if=.*\bof=/dev/"),
    re.compile(r"curl[^|]*\|\s*(sudo\s+)?(ba)?sh"),
    re.compile(r"wget[^|]*\|\s*(sudo\s+)?(ba)?sh"),
    re.compile(r"\bchmod\s+-R\s+777\s+/"),
]


def redact_secrets(text: str) -> tuple[str, bool]:
    redacted = False
    for pat in _SECRET_PATTERNS:
        text, n = pat.subn("[REDACTED]", text)
        redacted = redacted or n > 0
    return text, redacted


def looks_like_injection(text: str) -> bool:
    return injection_match(text) is not None


def injection_match(text: str) -> tuple[str, str] | None:
    """The first instruction-like phrase in ``text`` and a short excerpt around it, or None."""
    for p in _INJECTION_PATTERNS:
        m = p.search(text)
        if m:
            start, end = max(0, m.start() - 80), min(len(text), m.end() + 80)
            excerpt = " ".join(text[start:end].split())
            return m.group(0), ("…" if start else "") + excerpt + ("…" if end < len(text) else "")
    return None


def dangerous_shell(command: str) -> bool:
    return any(p.search(command) for p in _DANGEROUS_SHELL)


class GuardrailEngine:
    """Stateful enforcement for one run.

    Budgets are shared down a sub-council tree: a child engine is linked to its parent's, every
    spend is added at once to each ancestor's ``inflight`` (spend of descendants not yet charged
    back), and every engine checks the budget of each ancestor too. Parallel children therefore
    draw on one pool instead of each seeing the parent's whole remaining budget.
    """

    def __init__(
        self, rails: Guardrails, *, clock: Any = time.monotonic, parent: GuardrailEngine | None = None
    ) -> None:
        self.rails = rails
        self.clock = clock
        self.started = clock()
        # Seconds spent paused by the user, excluded from the runtime limit (set by RunContext).
        self.paused_s: Any = lambda: 0.0
        self.steps = 0
        self.usage = Usage()
        self.inflight = Usage()
        self.parent = parent

    # -------------------------------------------------- accounting
    def record_usage(self, usage: Usage) -> None:
        self.usage = self.usage + usage
        node = self.parent
        while node is not None:
            node.inflight = node.inflight + usage
            node = node.parent

    @property
    def total_usage(self) -> Usage:
        """This run's spend plus that of its running sub-councils."""
        return self.usage + self.inflight

    def settle_child(self, child: GuardrailEngine) -> Usage:
        """Charge a finished child's whole spend to this engine; returns the amount charged."""
        if child.parent is not self:
            return Usage()
        amount = child.total_usage
        self.inflight = _minus(self.inflight, amount)
        self.usage = self.usage + amount
        child.parent = None  # anything the child records later no longer reaches this tree
        return amount

    def record_step(self) -> None:
        self.steps += 1

    @property
    def runtime_s(self) -> float:
        return self.clock() - self.started - self.paused_s()

    def _budget_violation(self, scope: str = "") -> Violation | None:
        b, used = self.rails.budget, self.total_usage
        if b.usd is not None and used.cost_usd > b.usd:
            return Violation(
                "budget.usd", b.usd, round(used.cost_usd, 4), "halted", f"{scope}Spend budget exhausted"
            )
        if b.tokens is not None and used.total_tokens > b.tokens:
            return Violation(
                "budget.tokens", b.tokens, used.total_tokens, "halted", f"{scope}Token budget exhausted"
            )
        return None

    def check_limits(self) -> Violation | None:
        v = self._budget_violation()
        if v is not None:
            return v
        node = self.parent
        while node is not None:
            v = node._budget_violation("Parent council: ")
            if v is not None:
                return v
            node = node.parent
        if self.steps > self.rails.max_steps:
            return Violation("max_steps", self.rails.max_steps, self.steps, "halted", "Step limit reached")
        if self.runtime_s > self.rails.max_runtime_minutes * 60:
            return Violation(
                "max_runtime_minutes",
                self.rails.max_runtime_minutes,
                round(self.runtime_s / 60, 2),
                "halted",
                "Runtime limit reached",
            )
        return None

    def enforce_limits(self) -> None:
        v = self.check_limits()
        if v is not None:
            raise GuardrailBreach(v)

    def budget_snapshot(self) -> dict[str, Any]:
        used = self.total_usage
        return {
            "used": {
                "usd": round(used.cost_usd, 6),
                "tokens_in": used.input_tokens,
                "tokens_out": used.output_tokens,
                "steps": self.steps,
                "runtime_s": round(self.runtime_s, 1),
            },
            "limits": {
                "usd": self.rails.budget.usd,
                "tokens": self.rails.budget.tokens,
                "steps": self.rails.max_steps,
                "runtime_s": self.rails.max_runtime_minutes * 60,
            },
        }

    # -------------------------------------------------- tools
    def tool_allowed(self, tool: str, member_tools: list[str] | None = None) -> Violation | None:
        if _matches(tool, self.rails.tools.deny):
            return Violation("tools.deny", self.rails.tools.deny, tool, "blocked", f"Tool '{tool}' is denied")
        if not _matches(tool, self.rails.tools.allow):
            return Violation(
                "tools.allow", self.rails.tools.allow, tool, "blocked", f"Tool '{tool}' is not allowed"
            )
        if member_tools is not None and not _matches(tool, member_tools):
            return Violation(
                "member.tools", member_tools, tool, "blocked", f"Tool '{tool}' not granted to member"
            )
        return None

    def needs_approval(self, risk_tags: set[str]) -> list[str]:
        return sorted(risk_tags & set(self.rails.approval_required))

    def check_tool_args(self, tool: str, args: dict[str, Any]) -> Violation | None:
        for pattern in self.rails.tools.deny:
            # "shell:rm -rf*" style rules match against the command line.
            if ":" in pattern:
                name, _, argpat = pattern.partition(":")
                cmd = str(args.get("command", ""))
                if fnmatch.fnmatchcase(tool, name) and fnmatch.fnmatchcase(cmd, argpat):
                    return Violation(
                        "tools.deny", pattern, cmd[:200], "blocked", f"Blocked by rule '{pattern}'"
                    )
        if tool == "shell" and dangerous_shell(str(args.get("command", ""))):
            return Violation(
                "shell.dangerous",
                "destructive commands",
                str(args.get("command"))[:200],
                "blocked",
                "Destructive shell command blocked",
            )
        return None

    def normalize_path(self, path: str, *, write: bool) -> str:
        """Map ``path`` to a workspace-relative POSIX path or raise ``GuardrailBreach``."""
        root = self.rails.sandbox.root.rstrip("/")
        rel = path[len(root) + 1 :] if path.startswith(root + "/") else path.lstrip("/")
        parts = PurePosixPath(rel).parts
        if not rel or "\x00" in path or ".." in parts or "\\" in path:
            raise GuardrailBreach(
                Violation("sandbox.path", root, path[:200], "blocked", "Path must stay inside the workspace")
            )
        rel = str(PurePosixPath(*parts))
        if write:
            for ro in self.rails.sandbox.read_only_paths:
                ro_rel = ro[len(root) + 1 :] if ro.startswith(root + "/") else ro.lstrip("/")
                if (
                    fnmatch.fnmatchcase(rel, ro_rel)
                    or rel == ro_rel.rstrip("/")
                    or rel.startswith(ro_rel.rstrip("/*") + "/")
                ):
                    raise GuardrailBreach(
                        Violation("sandbox.read_only", ro, rel, "blocked", f"'{rel}' is read-only")
                    )
        return rel

    def check_file_size(self, size_bytes: int) -> Violation | None:
        limit = int(self.rails.sandbox.max_file_mb * 1024 * 1024)
        if size_bytes > limit:
            return Violation(
                "sandbox.max_file_mb",
                self.rails.sandbox.max_file_mb,
                round(size_bytes / 1048576, 2),
                "blocked",
                "File exceeds size limit",
            )
        return None

    # -------------------------------------------------- content
    def screen_text(self, text: str) -> list[Violation]:
        out: list[Violation] = []
        policy = self.rails.content_policy
        if policy.preset == "off":
            return out
        lowered = text.lower()
        for topic in policy.blocked_topics:
            if topic.lower() in lowered:
                out.append(
                    Violation("content.blocked_topic", topic, topic, "blocked", f"Blocked topic: {topic}")
                )
        if redact_secrets(text)[1]:
            out.append(
                Violation(
                    "content.secret",
                    "no secrets in output",
                    "secret-like token",
                    "truncated",
                    "Secret-like value redacted",
                )
            )
        if policy.preset == "strict" and looks_like_injection(text):
            out.append(
                Violation(
                    "content.injection",
                    "no instruction overrides",
                    "injection phrase",
                    "warned",
                    "Possible prompt-injection phrase",
                )
            )
        return out

    def screen_tool_output(self, text: str) -> tuple[str, list[Violation]]:
        """Tool output is untrusted data: redact secrets, flag injected instructions.

        A flagged phrase is only reported (``warned``): the output still reaches the agent, wrapped
        as untrusted data, because pages *about* prompt injection quote such phrases all the time.
        """
        cleaned, redacted = redact_secrets(text)
        found: list[Violation] = []
        if redacted:
            found.append(
                Violation("content.secret", "no secrets", "tool output", "truncated", "Secret redacted")
            )
        match = injection_match(cleaned)
        if match:
            phrase, excerpt = match
            found.append(
                Violation(
                    "content.injection",
                    "tool output is data",
                    phrase,
                    "warned",
                    "Tool output contains instruction-like text; the agent gets it as data, not as orders",
                    excerpt=excerpt,
                )
            )
        return cleaned, found
