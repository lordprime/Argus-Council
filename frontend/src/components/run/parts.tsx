import * as Popover from "@radix-ui/react-popover";
import {
  AlertTriangle,
  AtSign,
  BookOpen,
  Bot,
  Check,
  ChevronDown,
  ChevronRight,
  CircleStop,
  Eye,
  FileText,
  Globe,
  Hand,
  Link2,
  MessageSquareReply,
  Pause,
  Play,
  Plug,
  Plus,
  Send,
  ShieldAlert,
  ShieldX,
  Sparkles,
  Vote,
  Wrench,
  X,
} from "lucide-react";
import { useState, type ReactNode } from "react";
import { Link } from "react-router";

import { Badge, Button, IconButton, touchTarget } from "@/components/ui";
import { Markdown } from "@/components/ui/Markdown";
import { RoleAvatar, roleStyle } from "@/components/ui/role";
import { api, errorMessage, V1 } from "@/lib/api";
import { cn } from "@/lib/cn";
import { clock, compact, usd } from "@/lib/format";
import type { Member, Mode } from "@/lib/types";
import { BREAKPOINT, useMediaQuery } from "@/lib/useMediaQuery";
import type {
  ApprovalItem,
  Budget,
  InterceptInfo,
  PlanStep,
  RunState,
  SimpleItem,
  ThoughtItem,
  TimelineItem,
  ToolCallItem,
  VoteItem,
} from "@/stores/run";
import { useRuns } from "@/stores/run";
import { toast } from "@/stores/toast";

// ------------------------------------------------------------------ budget meter
/**
 * One budget microbar (spec §7.6): a visible label ("Budget", "Tokens", "Steps"), a 4px bar with
 * role="meter", and `used / cap`. Past 70% and 90% the bar turns warning and danger and an icon is
 * added, so the threshold is never carried by colour alone.
 */
function Meter({ label, noun, used, cap, fmt }: { label: string; noun: string; used: number; cap: number | null; fmt: (n: number) => string }) {
  const pct = cap ? Math.min(100, Math.round((used / cap) * 100)) : 0;
  const tone = pct >= 90 ? "bg-danger" : pct >= 70 ? "bg-warning" : "bg-accent";
  return (
    <div className="flex items-center gap-2 text-xs tabular">
      <span className="w-12 shrink-0 text-xs text-fg-subtle">{label}</span>
      <div role="meter" aria-label={`${noun[0]!.toUpperCase()}${noun.slice(1)} budget`} aria-valuemin={0} aria-valuemax={100} aria-valuenow={pct}
        aria-valuetext={cap ? `${pct} percent of ${noun} budget, ${fmt(used)} of ${fmt(cap)}` : `${fmt(used)} used, no ${noun} cap`}
        className="h-1 w-20 sm:w-[7.5rem] shrink-0 rounded-full bg-surface-3 overflow-hidden">
        <div className={cn("h-full", tone)} style={{ width: `${pct}%` }} />
      </div>
      <span className={cn("inline-flex items-center gap-1 whitespace-nowrap", pct >= 90 ? "text-danger" : pct >= 70 ? "text-warning" : "text-fg-muted")}>
        {fmt(used)}{cap ? ` / ${fmt(cap)}` : ""}
        {pct >= 70 && <AlertTriangle className="size-3" aria-hidden />}
      </span>
    </div>
  );
}

export function BudgetMeter({ budget }: { budget: Budget | null }) {
  if (!budget) return null;
  const tokens = budget.used.tokens_in + budget.used.tokens_out;
  return (
    <div role="group" aria-label="Budget usage" className="flex flex-col gap-0.5">
      <Meter label="Budget" noun="dollar" used={budget.used.usd} cap={budget.limits.usd} fmt={(n) => usd(n)} />
      <Meter label="Tokens" noun="token" used={tokens} cap={budget.limits.tokens} fmt={compact} />
      <Meter label="Steps" noun="step" used={budget.used.steps} cap={budget.limits.steps} fmt={(n) => String(n)} />
    </div>
  );
}

// ------------------------------------------------------------------ council rail
const STATUS_LINE: Record<string, string> = {
  idle: "idle", thinking: "thinking…", tool: "using tool", waiting: "waiting for approval", held: "held", blocked: "blocked", error: "error",
};

export function CouncilRail({ runId, members, state, live, onFocus, focused }: {
  runId: string;
  members: Member[];
  state: RunState;
  live: boolean;
  onFocus: (memberId: string | null) => void;
  focused: string | null;
}) {
  const setStatus = useRuns((s) => s.setMemberStatus);
  const hold = async (m: Member, release: boolean) => {
    setStatus(runId, m.id, release ? "idle" : "held");
    try {
      await api.post(`${V1}/runs/${runId}/interventions`, { kind: release ? "release" : "hold", target: { member_id: m.id } });
    } catch (err) {
      toast({ tone: "danger", title: release ? "Could not release" : "Could not hold", body: errorMessage(err) });
    }
  };
  const groups: [string, Member[]][] = [
    ["Chair", members.filter((m) => m.role === "chair")],
    ["Workers", members.filter((m) => m.role !== "chair" && !m.config?.overseer)],
    ["Overseers", members.filter((m) => m.config?.overseer)],
  ];
  return (
    <aside aria-label="Council" className="flex flex-col gap-4 p-3 overflow-auto">
      {groups.filter(([, ms]) => ms.length).map(([title, ms]) => (
        <section key={title}>
          <h2 className="text-2xs uppercase tracking-wide text-fg-subtle mb-1.5 px-1">{title}</h2>
          <ul className="flex flex-col gap-1.5">
            {ms.map((m) => {
              const st = state.memberStatus[m.id]?.status ?? "idle";
              const detail = state.memberStatus[m.id]?.detail;
              const spend = state.budget?.by_member?.[m.id];
              const s = roleStyle(m.role);
              return (
                <li key={m.id}
                  className={cn("rounded-lg border bg-surface p-2.5 border-l-[3px]", focused === m.id ? "border-accent" : "border-border",
                    s.border)}>
                  <div className="flex items-center gap-2">
                    <RoleAvatar role={m.role} overseer={Boolean(m.config?.overseer)} />
                    <div className="min-w-0 flex-1">
                      <p className="text-sm font-semibold truncate">{m.name}</p>
                      <p className="text-2xs font-mono text-fg-subtle truncate">{m.provider}/{m.model}</p>
                    </div>
                  </div>
                  <p className={cn("text-xs mt-1.5 flex items-center gap-1",
                    st === "held" || st === "waiting" ? "text-warning" : st === "blocked" || st === "error" ? "text-danger" : "text-fg-muted")}>
                    {st === "thinking" && <span className="size-1.5 rounded-full bg-info animate-pulse" aria-hidden />}
                    {STATUS_LINE[st] ?? st}{detail && st === "tool" ? `: ${detail}` : ""}
                    {spend && <span className="ml-auto tabular text-fg-subtle">{usd(spend.usd)}</span>}
                  </p>
                  <div className="flex gap-1 mt-1.5">
                    <Button size="sm" variant="ghost" icon={Eye} onClick={() => onFocus(focused === m.id ? null : m.id)} aria-pressed={focused === m.id}>
                      {focused === m.id ? "Unfocus" : "Focus"}
                    </Button>
                    {live && (st === "held" ? (
                      <Button size="sm" variant="ghost" icon={Play} onClick={() => void hold(m, true)}>Release</Button>
                    ) : (
                      <Button size="sm" variant="ghost" icon={Hand} onClick={() => void hold(m, false)}>Hold</Button>
                    ))}
                  </div>
                </li>
              );
            })}
          </ul>
        </section>
      ))}
    </aside>
  );
}

// ------------------------------------------------------------------ timeline items
function memberName(members: Member[], id?: string) {
  return members.find((m) => m.id === id)?.name ?? "Agent";
}

/** Friendly titles for the guardrail rules users meet most; anything else shows its rule id. */
const VIOLATION_TITLE: Record<string, string> = {
  "content.injection": "Possible prompt injection in tool output",
  "content.secret": "Secret redacted",
  "content.blocked_topic": "Blocked topic",
};

/**
 * A guardrail event. Only what stopped something (blocked, halted) is red; a warning or a redaction is
 * the guardrail doing its job and reads as amber. One line by default, details on expand.
 */
function ViolationRow({ d }: { d: Record<string, any> }) { // eslint-disable-line @typescript-eslint/no-explicit-any
  const stopped = d.action === "blocked" || d.action === "halted";
  const Icon = stopped ? ShieldX : ShieldAlert;
  const target = (d.target ?? {}) as { tool?: string; source?: string };
  const details: [string, ReactNode][] = [
    ["Rule", <code key="r">{d.rule}</code>],
    ["Action", d.action],
    ["Limit", String(d.limit)],
    ["Observed", String(d.observed)],
    ...(target.tool ? [["Tool", <code key="t">{target.tool}</code>] as [string, ReactNode]] : []),
    ...(target.source
      ? [["Source", <a key="s" href={target.source} target="_blank" rel="noopener noreferrer nofollow" className="text-accent underline break-all">{target.source}</a>] as [string, ReactNode]]
      : []),
  ];
  return (
    <div role="status">
    <details className={cn("group rounded-md border px-3 py-2 text-sm", stopped ? "border-danger bg-danger-subtle" : "border-warning/60 bg-warning-subtle/60")}
      data-violation={d.action}>
      <summary className="flex cursor-pointer list-none items-start gap-2 [&::-webkit-details-marker]:hidden">
        <Icon className={cn("size-4 mt-0.5 shrink-0", stopped ? "text-danger" : "text-warning")} aria-hidden />
        <span className="min-w-0 flex-1">
          <strong className="font-medium">{VIOLATION_TITLE[d.rule] ?? d.rule}</strong>
          <span className="text-fg-muted">{" · "}{d.action}{target.tool ? ` · ${target.tool}` : ""}</span>
          <span className="block text-fg-muted">{d.message}</span>
        </span>
        <ChevronRight className="size-4 mt-0.5 shrink-0 text-fg-subtle transition-transform group-open:rotate-90" aria-hidden />
      </summary>
      <dl className="mt-2 grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1 pl-6 text-xs">
        {details.map(([k, v]) => (
          <div key={k} className="contents"><dt className="text-fg-subtle">{k}</dt><dd className="min-w-0 break-words">{v}</dd></div>
        ))}
      </dl>
      {d.excerpt && (
        <blockquote className="mt-2 ml-6 border-l-2 border-border-strong pl-2 text-xs text-fg-muted break-words">{d.excerpt}</blockquote>
      )}
    </details>
    </div>
  );
}

function Row({ icon: Icon, tone, children }: { icon: typeof Bot; tone?: string; children: ReactNode }) {
  return (
    <div className={cn("flex items-start gap-2 text-sm px-1", tone ?? "text-fg-muted")}>
      <Icon className="size-4 mt-0.5 shrink-0" aria-hidden />
      <div className="min-w-0 flex-1">{children}</div>
    </div>
  );
}

export function PlanCard({ plan, members }: { plan: { version: number; steps: PlanStep[] }; members: Member[] }) {
  return (
    <div className="rounded-lg border border-border bg-surface p-3">
      <p className="text-sm font-semibold mb-2">Plan <span className="text-fg-subtle font-normal">v{plan.version}</span></p>
      <ol className="flex flex-col gap-1">
        {plan.steps.map((s, i) => {
          const m = members.find((x) => x.id === s.assignee_member_id);
          return (
            <li key={s.id} className="flex items-center gap-2 text-sm">
              <span className="w-5 text-fg-subtle tabular">{i + 1}.</span>
              {m && <RoleAvatar role={m.role} size={18} label={m.name} />}
              <span className="flex-1 truncate">{s.title}</span>
              <Badge tone={s.status === "done" ? "success" : s.status === "running" ? "info" : s.status === "failed" ? "danger" : "neutral"}>
                {s.status}
              </Badge>
            </li>
          );
        })}
      </ol>
    </div>
  );
}

function InterceptThread({ i }: { i: InterceptInfo }) {
  const tone = i.action === "block" ? "border-danger" : i.action === "hold" ? "border-warning" : "border-info";
  return (
    <div className={cn("ml-6 mt-2 border-l-2 pl-3 py-1 text-sm", tone)}>
      <div className="flex flex-wrap items-center gap-1.5">
        <RoleAvatar role={i.actor.role ?? (i.by === "user" ? "user" : "monitor")} overseer size={18} label={i.actor.name} />
        <span className="font-medium">{i.actor.name ?? i.by}</span>
        <Badge tone={i.action === "block" ? "danger" : i.action === "hold" ? "warning" : "info"}>{i.action}</Badge>
        <Badge>{i.reason}</Badge>
        {i.note && <span className="text-fg-muted">{i.note}</span>}
      </div>
      {i.correction && <p className="mt-1 text-fg"><MessageSquareReply className="inline size-3.5 mr-1" aria-hidden />{i.correction}</p>}
    </div>
  );
}

function CorrectionComposer({ runId, thought, onDone }: { runId: string; thought: ThoughtItem; onDone: () => void }) {
  const [text, setText] = useState("");
  const [scope, setScope] = useState<"thought" | "member" | "council">("thought");
  const [busy, setBusy] = useState(false);
  const chips = ["Stay on topic", "Cite a source", "Take a smaller step", "Check with me first"];
  const send = async () => {
    if (!text.trim()) return;
    setBusy(true);
    try {
      await api.post(`${V1}/runs/${runId}/interventions`, {
        kind: "correct", text, scope: scope === "council" ? "council" : scope,
        target: scope === "council" ? {} : { member_id: thought.actor.member_id, thought_id: thought.id },
      });
      toast({ tone: "success", title: "Correction sent" });
      onDone();
    } catch (err) {
      toast({ tone: "danger", title: "Could not send correction", body: errorMessage(err) });
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="ml-6 mt-2 rounded-lg border border-warning bg-warning-subtle/40 p-3 flex flex-col gap-2">
      <div className="flex flex-wrap gap-1.5">
        {chips.map((c) => <Button key={c} size="sm" type="button" onClick={() => setText((t) => (t ? `${t} ${c}.` : `${c}.`))}>{c}</Button>)}
      </div>
      <label className="sr-only" htmlFor={`corr-${thought.id}`}>Correction</label>
      <textarea id={`corr-${thought.id}`} autoFocus value={text} onChange={(e) => setText(e.target.value)} rows={2}
        onKeyDown={(e) => { if ((e.metaKey || e.ctrlKey) && e.key === "Enter") void send(); if (e.key === "Escape") onDone(); }}
        className="w-full rounded-md border border-border-strong bg-surface p-2 text-sm" placeholder="Tell the agent what to change" />
      <div className="flex flex-wrap items-center gap-3 text-sm">
        {(["thought", "member", "council"] as const).map((s) => (
          <label key={s} className="flex items-center gap-1">
            <input type="radio" name={`scope-${thought.id}`} checked={scope === s} onChange={() => setScope(s)} />
            {s === "thought" ? "This thought" : s === "member" ? `Standing order for ${thought.actor.name}` : "Standing order for council"}
          </label>
        ))}
        <div className="ml-auto flex gap-2">
          <Button size="sm" variant="ghost" onClick={onDone}>Cancel</Button>
          <Button size="sm" variant="primary" icon={Send} loading={busy} onClick={() => void send()}>Send correction</Button>
        </div>
      </div>
    </div>
  );
}

/**
 * A ballot an agent wrote as its message: `{"option": "approve", "rationale": "…"}`, optionally in a
 * ```json fence. Returns null for anything else, so ordinary messages render as Markdown (audit C-26).
 */
export function parseVoteText(text: string): { option: string; rationale?: string } | null {
  const body = text.trim().replace(/^```(?:json)?\s*/i, "").replace(/\s*```$/, "").trim();
  if (!body.startsWith("{") || !body.endsWith("}")) return null;
  try {
    const v = JSON.parse(body) as { option?: unknown; rationale?: unknown };
    if (!v || typeof v !== "object" || typeof v.option !== "string" || !v.option.trim()) return null;
    return { option: v.option, rationale: typeof v.rationale === "string" && v.rationale.trim() ? v.rationale.trim() : undefined };
  } catch {
    return null;
  }
}

export function ThoughtCard({ runId, t, live, selected, onSelect }: {
  runId: string; t: ThoughtItem; live: boolean; selected: boolean; onSelect: () => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const [correcting, setCorrecting] = useState(false);
  const s = roleStyle(t.actor.role);
  const held = t.intercepts.some((i) => i.action === "hold" && i.outcome === "pending" && !i.correction);
  const intercept = async () => {
    try {
      await api.post(`${V1}/runs/${runId}/interventions`, { kind: "intercept", target: { member_id: t.actor.member_id, thought_id: t.id } });
      setCorrecting(true);
    } catch (err) {
      toast({ tone: "danger", title: "Could not intercept", body: errorMessage(err) });
    }
  };
  const vote = t.streaming ? null : parseVoteText(t.text);
  const long = !vote && (t.text.length > 400 || t.text.split("\n").length > 4);
  return (
    <div className={cn("rounded-lg border bg-surface border-l-[3px] p-3", selected ? "outline outline-2 outline-accent" : "",
      held ? "border-warning" : "border-border", s.border)}>
      <div className="flex items-center gap-2">
        <RoleAvatar role={t.actor.role} overseer={t.actor.overseer} />
        <button type="button" className="min-h-6 text-sm font-semibold hover:underline" onClick={onSelect}>{t.actor.name}</button>
        <span className="text-xs text-fg-subtle tabular">{clock(t.ts)}</span>
        {held && <Badge tone="warning">held</Badge>}
        {!t.streaming && t.tokensOut != null && (
          <span className="text-xs text-fg-subtle tabular ml-auto">{compact((t.tokensIn ?? 0) + t.tokensOut)} tok · {usd(t.costUsd)}</span>
        )}
      </div>
      <div className={cn("mt-2 text-base", !expanded && long && "line-clamp-4", t.streaming && "caret")}>
        {vote ? (
          <p className="flex items-start gap-1.5">
            <Vote className="size-4 mt-1 shrink-0 text-fg-subtle" aria-hidden />
            <span>Voted <strong className="font-semibold">{vote.option}</strong>{vote.rationale && <span className="text-fg-muted"> · {vote.rationale}</span>}</span>
          </p>
        ) : t.text ? <Markdown className="text-base">{t.text}</Markdown> : t.streaming ? <span className="text-fg-subtle">thinking</span> : (
          <span className="text-fg-subtle italic">(used tools)</span>
        )}
      </div>
      <div className="flex gap-1 mt-1">
        {long && <Button size="sm" variant="ghost" icon={expanded ? ChevronDown : ChevronRight} onClick={() => setExpanded((e) => !e)}>{expanded ? "Less" : "More"}</Button>}
        {live && <Button size="sm" variant="ghost" icon={Hand} onClick={() => void intercept()}>Intercept</Button>}
        {live && <Button size="sm" variant="ghost" icon={MessageSquareReply} onClick={() => setCorrecting((c) => !c)}>Correct</Button>}
        <Button size="sm" variant="ghost" onClick={onSelect}>Inspect</Button>
      </div>
      {t.intercepts.map((i) => <InterceptThread key={i.id} i={i} />)}
      {correcting && <CorrectionComposer runId={runId} thought={t} onDone={() => setCorrecting(false)} />}
    </div>
  );
}

const SOURCE_ICON: Record<string, typeof Wrench> = { builtin: Wrench, mcp: Plug, skill: Sparkles, a2a: Globe, acp: Globe };

export function ToolChip({ t, onSelect }: { t: ToolCallItem; onSelect: () => void }) {
  const [open, setOpen] = useState(false);
  const Icon = SOURCE_ICON[t.source] ?? Wrench;
  const tone = t.status === "ok" ? "success" : t.status === "running" ? "info" : t.status === "blocked" ? "warning" : "danger";
  const argSummary = JSON.stringify(t.args ?? {}).slice(0, 80);
  return (
    <div className="ml-6 rounded-md border border-border bg-surface-2 text-sm">
      <button className="w-full flex items-center gap-2 px-2.5 h-8 text-left" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
        <Icon className="size-3.5 text-fg-subtle shrink-0" aria-label={t.source} />
        <span className="font-mono truncate">{t.tool}<span className="text-fg-subtle">({argSummary})</span></span>
        <Badge tone={tone} className="ml-auto">{t.status}</Badge>
        {t.durationMs != null && <span className="text-xs text-fg-subtle tabular">{t.durationMs}ms</span>}
      </button>
      {open && (
        <div className="border-t border-border p-2.5 flex flex-col gap-2">
          <pre tabIndex={0} role="region" aria-label="Arguments" className="font-mono text-xs whitespace-pre-wrap break-all max-h-40 overflow-auto">{JSON.stringify(t.args, null, 2)}</pre>
          {(t.output || t.preview) && (
            <pre tabIndex={0} role="region" aria-label="Output" className="font-mono text-xs whitespace-pre-wrap max-h-[400px] overflow-auto bg-surface rounded-md p-2">{t.output || t.preview}</pre>
          )}
          <Button size="sm" variant="ghost" onClick={onSelect} className="self-start">Open in inspector</Button>
        </div>
      )}
    </div>
  );
}

export function ApprovalCard({ runId, a }: { runId: string; a: ApprovalItem }) {
  const patch = useRuns((s) => s.patchItem);
  const [busy, setBusy] = useState<"approve" | "reject" | null>(null);
  const [reason, setReason] = useState("");
  const decide = async (decision: "approve" | "reject") => {
    setBusy(decision);
    try {
      await api.post(`${V1}/runs/${runId}/approvals/${a.id}`, { decision, ...(reason ? { reason } : {}) });
      patch(runId, a.id, { status: decision === "approve" ? "approved" : "rejected" } as Partial<TimelineItem>);
    } catch (err) {
      toast({ tone: "danger", title: "Could not record decision", body: errorMessage(err) });
    } finally {
      setBusy(null);
    }
  };
  // In replay (runId "replay:<id>") the card shows history: a pending approval cannot be decided from here.
  const replaying = runId.startsWith("replay:");
  const pending = a.status === "pending";
  const actionable = pending && !replaying;
  return (
    <div role={actionable ? "alert" : undefined}
      className={cn("rounded-lg border-2 p-3", pending ? "border-warning bg-warning-subtle" : "border-border bg-surface")}>
      <div className="flex items-center gap-2">
        <AlertTriangle className="size-4 text-warning" aria-hidden />
        <p className="text-sm font-semibold">
          {actionable ? "Approval needed" : pending ? "Awaiting approval at this point in the replay" : `Approval ${a.status}`}
        </p>
        {a.risk.map((r) => <Badge key={r} tone="warning">{r}</Badge>)}
      </div>
      <p className="mt-1.5 text-sm font-mono break-all">{a.summary}</p>
      {actionable && (
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <label className="sr-only" htmlFor={`reason-${a.id}`}>Reason (optional)</label>
          <input id={`reason-${a.id}`} value={reason} onChange={(e) => setReason(e.target.value)} placeholder="Reason (optional)"
            className={cn("h-9 flex-1 min-w-40 rounded-md border border-border-strong bg-surface px-2 text-sm", touchTarget)} />
          <Button variant="primary" icon={Check} loading={busy === "approve"} onClick={() => void decide("approve")}>Approve</Button>
          <Button variant="danger" icon={X} loading={busy === "reject"} onClick={() => void decide("reject")}>Reject</Button>
        </div>
      )}
    </div>
  );
}

export function VoteCard({ v, members }: { v: VoteItem; members: Member[] }) {
  const total = v.casts.length || 1;
  return (
    <div className="rounded-lg border border-border bg-surface p-3">
      <p className="text-sm font-semibold flex items-center gap-2"><Vote className="size-4" aria-hidden />{v.motion}</p>
      <ul className="mt-2 flex flex-col gap-1.5">
        {v.options.map((o) => {
          const n = v.casts.filter((c) => c.option === o.id).length;
          return (
            <li key={o.id} className="text-sm">
              <div className="flex justify-between"><span>{o.label}{v.result === o.id ? " ✓ (carried)" : ""}</span><span className="tabular">{n}</span></div>
              <div className="h-1.5 rounded-full bg-surface-3 overflow-hidden"><div className="h-full bg-accent" style={{ width: `${(n / total) * 100}%` }} /></div>
            </li>
          );
        })}
      </ul>
      {v.casts.length > 0 && (
        <details className="mt-2 text-sm">
          <summary className="cursor-pointer text-fg-muted">Rationales ({v.casts.length})</summary>
          <ul className="mt-1 flex flex-col gap-1">
            {v.casts.map((c) => (
              <li key={c.memberId} className="flex gap-2"><RoleAvatar role={c.actor.role} size={18} label={memberName(members, c.memberId)} />
                <span><strong>{c.actor.name}</strong> · {v.options.find((o) => o.id === c.option)?.label ?? c.option}{c.rationale ? `: ${c.rationale}` : ""}</span></li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}

export function SimpleRow({ item, onOpenArtifact }: { item: SimpleItem; onOpenArtifact: (path: string) => void }) {
  const d = item.data as Record<string, any>; // eslint-disable-line @typescript-eslint/no-explicit-any
  switch (item.kind) {
    case "artifact":
      return (
        <Row icon={FileText}>
          <button className="hover:underline font-mono" onClick={() => onOpenArtifact(d.path)}>{d.path}</button>{" "}
          {item.type === "artifact.deleted" ? <Badge tone="danger">deleted</Badge> : <span className="text-fg-subtle">v{d.version}
            {d.diff_stats ? ` · +${d.diff_stats.added} −${d.diff_stats.removed}` : ""}</span>}
        </Row>
      );
    case "violation":
      return <ViolationRow d={d} />;
    case "citation":
      return <Row icon={Link2}>Source {d.n}: <a href={d.url} target="_blank" rel="noopener noreferrer nofollow" className="text-accent underline">{d.title}</a></Row>;
    case "user":
      return (
        <div className="ml-auto max-w-[80%] rounded-lg bg-accent-subtle px-3 py-2 text-sm">
          <p className="text-2xs uppercase text-fg-subtle">{item.type === "correction.issued" ? `Correction · ${d.scope}` : "You"}</p>
          <p>{d.text}</p>
        </div>
      );
    case "error":
      return <Row icon={AlertTriangle} tone="text-danger">{d.message}</Row>;
    case "system":
      if (item.type === "run.status") return <Row icon={d.status === "paused" ? Pause : Bot}>Run {d.status}{d.reason ? `: ${d.reason}` : ""}</Row>;
      if (item.type === "correction.resolved") return <Row icon={Check}>Correction {d.outcome}</Row>;
      if (item.type === "skill.loaded") return <Row icon={Sparkles}>Loaded skill <span className="font-mono">{d.name}</span></Row>;
      if (item.type === "knowledge.linked") {
        const names = ((d.names as string[] | undefined) ?? []).join(", ") || "a knowledge base";
        return <Row icon={BookOpen}>{d.action === "unlinked" ? "Unlinked" : "Linked"} knowledge: <span className="font-medium">{names}</span></Row>;
      }
      if (item.type === "intercept.raised") return <Row icon={Hand} tone="text-warning">{item.actor.name} intercepted: {d.note ?? d.reason}</Row>;
      return <Row icon={Bot}>{item.type}</Row>;
    default:
      return null;
  }
}

// ------------------------------------------------------------------ intervention bar
const FINISHED: Record<string, string> = {
  completed: "Run completed", failed: "Run failed", halted: "Run halted", cancelled: "Run stopped",
};

/** What a "Start a follow-up run" link hands the Home composer (routes/Home.tsx ComposerPrefill). */
export interface FollowUp { topic: string; mode: Mode; description?: string }

/**
 * Bottom bar of the run workspace. Live: target and message type, the message, Send and Halt all; below
 * `sm` it is a single row and the two selects move into a popover (audit C-04). Finished: a one-line
 * "Run finished · Start a follow-up run" row instead of a bar of disabled controls (C-16).
 */
export function InterventionBar({ runId, members, disabled, status, followUp }: {
  runId: string;
  members: Member[];
  disabled: boolean;
  /** Terminal status, to word the finished row. */
  status?: string;
  followUp?: FollowUp;
}) {
  const [target, setTarget] = useState("council");
  const [text, setText] = useState("");
  const [kind, setKind] = useState<"message" | "correct">("message");
  const [busy, setBusy] = useState(false);
  const wide = useMediaQuery(BREAKPOINT.sm);
  if (disabled) {
    return (
      <div className="border-t border-border bg-surface px-3 py-2 flex flex-wrap items-center gap-x-2 gap-y-1 text-sm">
        <span className="inline-flex items-center gap-1.5 text-fg-muted">
          <CircleStop className="size-4 shrink-0" aria-hidden />{FINISHED[status ?? ""] ?? "Run finished"}
        </span>
        <span aria-hidden className="text-fg-subtle">·</span>
        <Link to="/" state={followUp ? { prefill: followUp } : undefined}
          className={cn("inline-flex items-center gap-1 min-h-6 font-medium text-accent underline underline-offset-2", touchTarget)}>
          <Plus className="size-4" aria-hidden />Start a follow-up run
        </Link>
      </div>
    );
  }
  const send = async () => {
    if (!text.trim()) return;
    setBusy(true);
    try {
      const body = kind === "message"
        ? { kind, text, target: target === "council" ? {} : { member_id: target } }
        : { kind, text, scope: target === "council" ? "council" : "member", target: target === "council" ? {} : { member_id: target } };
      await api.post(`${V1}/runs/${runId}/interventions`, body);
      setText("");
    } catch (err) {
      toast({ tone: "danger", title: "Could not send", body: errorMessage(err) });
    } finally {
      setBusy(false);
    }
  };
  const haltAll = async () => {
    if (!window.confirm("Hold every agent? They stop at their next step until released.")) return;
    try {
      await api.post(`${V1}/runs/${runId}/interventions`, { kind: "halt_all" });
      toast({ tone: "warning", title: "All agents held" });
    } catch (err) {
      toast({ tone: "danger", title: "Could not halt", body: errorMessage(err) });
    }
  };
  const targetName = target === "council" ? "Council" : members.find((m) => m.id === target)?.name ?? "agent";
  const select = cn("h-9 rounded-md border border-border-strong bg-surface px-2 text-sm", touchTarget);
  const selects = (
    <>
      <label className={wide ? "sr-only" : "text-sm font-medium"} htmlFor="iv-target">Send to</label>
      <select id="iv-target" value={target} onChange={(e) => setTarget(e.target.value)} className={select}>
        <option value="council">Council</option>
        {members.map((m) => <option key={m.id} value={m.id}>@{m.name}</option>)}
      </select>
      <label className={wide ? "sr-only" : "text-sm font-medium mt-1"} htmlFor="iv-kind">Type</label>
      <select id="iv-kind" value={kind} onChange={(e) => setKind(e.target.value as "message" | "correct")} className={select}>
        <option value="message">Message</option>
        <option value="correct">Standing order</option>
      </select>
    </>
  );
  return (
    <form aria-label="Intervene" onSubmit={(e) => { e.preventDefault(); void send(); }}
      className="border-t border-border bg-surface p-2 flex items-center gap-2">
      {wide ? selects : (
        <Popover.Root>
          <Popover.Trigger asChild>
            {/* Visible text (the target) starts the name, then what the button does. */}
            <Button type="button" className="shrink-0 max-w-[8rem]"
              aria-label={`${targetName}, ${kind === "message" ? "message" : "standing order"}: change recipient or type`}>
              <AtSign className="size-4 shrink-0" aria-hidden />
              <span className="truncate">{targetName}</span>
              <ChevronDown className="size-3.5 shrink-0" aria-hidden />
            </Button>
          </Popover.Trigger>
          <Popover.Portal>
            <Popover.Content side="top" align="start" sideOffset={6} aria-label="Message options"
              className="z-popover w-64 max-w-[calc(100vw-1rem)] rounded-lg border border-border bg-surface shadow-2 p-3 flex flex-col gap-1">
              {selects}
            </Popover.Content>
          </Popover.Portal>
        </Popover.Root>
      )}
      <label className="sr-only" htmlFor="iv-text">Message</label>
      <input id="iv-text" value={text} onChange={(e) => setText(e.target.value)} enterKeyHint="send"
        placeholder={wide ? "Message the council, or give a standing order…" : `Message ${target === "council" ? "the council" : targetName}…`}
        className={cn("h-9 flex-1 min-w-0 rounded-md border border-border-strong bg-surface px-3 text-sm", touchTarget)} />
      <Button type="submit" variant="primary" icon={Send} loading={busy} disabled={!text.trim()} className="shrink-0">
        <span className="sr-only sm:not-sr-only">Send</span>
      </Button>
      <IconButton type="button" label="Halt all agents" icon={Hand} variant="danger-ghost" size="md" className="shrink-0" onClick={() => void haltAll()} />
    </form>
  );
}
