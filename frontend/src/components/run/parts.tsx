import {
  AlertTriangle,
  Bot,
  Check,
  ChevronDown,
  ChevronRight,
  Eye,
  FileText,
  Globe,
  Hand,
  Link2,
  MessageSquareReply,
  Pause,
  Play,
  Plug,
  Send,
  ShieldX,
  Sparkles,
  Vote,
  Wrench,
  X,
} from "lucide-react";
import { useState, type ReactNode } from "react";

import { Badge, Button, IconButton } from "@/components/ui";
import { Markdown } from "@/components/ui/Markdown";
import { RoleAvatar, roleStyle } from "@/components/ui/role";
import { api, errorMessage, V1 } from "@/lib/api";
import { cn } from "@/lib/cn";
import { clock, compact, usd } from "@/lib/format";
import type { Member } from "@/lib/types";
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
function Meter({ label, used, cap, fmt }: { label: string; used: number; cap: number | null; fmt: (n: number) => string }) {
  const pct = cap ? Math.min(100, Math.round((used / cap) * 100)) : 0;
  const tone = pct >= 90 ? "bg-danger" : pct >= 70 ? "bg-warning" : "bg-accent";
  return (
    <div className="flex items-center gap-2 text-xs tabular">
      <span className="w-12 text-fg-subtle">{label}</span>
      <div role="meter" aria-label={`${label} budget`} aria-valuemin={0} aria-valuemax={100} aria-valuenow={pct}
        aria-valuetext={cap ? `${pct} percent of ${label} budget` : `${fmt(used)} used, no cap`}
        className="h-1 w-24 rounded-full bg-surface-3 overflow-hidden">
        <div className={cn("h-full", tone)} style={{ width: `${pct}%` }} />
      </div>
      <span className="text-fg-muted whitespace-nowrap">{fmt(used)}{cap ? ` / ${fmt(cap)}` : ""}{pct >= 70 ? (pct >= 90 ? " ⚠" : " !") : ""}</span>
    </div>
  );
}

export function BudgetMeter({ budget }: { budget: Budget | null }) {
  if (!budget) return null;
  const tokens = budget.used.tokens_in + budget.used.tokens_out;
  return (
    <div className="flex flex-col gap-0.5" aria-label="Budget">
      <Meter label="$" used={budget.used.usd} cap={budget.limits.usd} fmt={(n) => usd(n)} />
      <Meter label="tokens" used={tokens} cap={budget.limits.tokens} fmt={compact} />
      <Meter label="steps" used={budget.used.steps} cap={budget.limits.steps} fmt={(n) => String(n)} />
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
  const long = t.text.length > 400 || t.text.split("\n").length > 4;
  return (
    <div className={cn("rounded-lg border bg-surface border-l-[3px] p-3", selected ? "outline outline-2 outline-accent" : "",
      held ? "border-warning" : "border-border", s.border)}>
      <div className="flex items-center gap-2">
        <RoleAvatar role={t.actor.role} overseer={t.actor.overseer} />
        <button className="text-sm font-semibold hover:underline" onClick={onSelect}>{t.actor.name}</button>
        <span className="text-xs text-fg-subtle tabular">{clock(t.ts)}</span>
        {held && <Badge tone="warning">held</Badge>}
        {!t.streaming && t.tokensOut != null && (
          <span className="text-xs text-fg-subtle tabular ml-auto">{compact((t.tokensIn ?? 0) + t.tokensOut)} tok · {usd(t.costUsd)}</span>
        )}
      </div>
      <div className={cn("mt-2 text-base", !expanded && long && "line-clamp-4", t.streaming && "caret")}>
        {t.text ? <Markdown className="text-base">{t.text}</Markdown> : t.streaming ? <span className="text-fg-subtle">thinking</span> : (
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
  const pending = a.status === "pending";
  return (
    <div role={pending ? "alert" : undefined}
      className={cn("rounded-lg border-2 p-3", pending ? "border-warning bg-warning-subtle" : "border-border bg-surface")}>
      <div className="flex items-center gap-2">
        <AlertTriangle className="size-4 text-warning" aria-hidden />
        <p className="text-sm font-semibold">{pending ? "Approval needed" : `Approval ${a.status}`}</p>
        {a.risk.map((r) => <Badge key={r} tone="warning">{r}</Badge>)}
      </div>
      <p className="mt-1.5 text-sm font-mono break-all">{a.summary}</p>
      {pending && (
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <label className="sr-only" htmlFor={`reason-${a.id}`}>Reason (optional)</label>
          <input id={`reason-${a.id}`} value={reason} onChange={(e) => setReason(e.target.value)} placeholder="Reason (optional)"
            className="h-9 flex-1 min-w-40 rounded-md border border-border-strong bg-surface px-2 text-sm" />
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
                <span><strong>{c.actor.name}</strong> · {c.option}: {c.rationale}</span></li>
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
      return (
        <div className="rounded-md border border-danger bg-danger-subtle px-3 py-2 text-sm flex items-start gap-2" role="status">
          <ShieldX className="size-4 text-danger mt-0.5" aria-hidden />
          <div><strong>{d.rule}</strong>: {d.message} <span className="text-fg-muted">(limit {String(d.limit)}, observed {String(d.observed)}, {d.action})</span></div>
        </div>
      );
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
      if (item.type === "intercept.raised") return <Row icon={Hand} tone="text-warning">{item.actor.name} intercepted: {d.note ?? d.reason}</Row>;
      return <Row icon={Bot}>{item.type}</Row>;
    default:
      return null;
  }
}

// ------------------------------------------------------------------ intervention bar
export function InterventionBar({ runId, members, disabled }: { runId: string; members: Member[]; disabled: boolean }) {
  const [target, setTarget] = useState("council");
  const [text, setText] = useState("");
  const [kind, setKind] = useState<"message" | "correct">("message");
  const [busy, setBusy] = useState(false);
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
  return (
    <form aria-label="Intervene" onSubmit={(e) => { e.preventDefault(); void send(); }}
      className="border-t border-border bg-surface p-2 flex flex-wrap items-center gap-2">
      <label className="sr-only" htmlFor="iv-target">Send to</label>
      <select id="iv-target" value={target} onChange={(e) => setTarget(e.target.value)} disabled={disabled}
        className="h-9 rounded-md border border-border-strong bg-surface px-2 text-sm">
        <option value="council">Council</option>
        {members.map((m) => <option key={m.id} value={m.id}>@{m.name}</option>)}
      </select>
      <label className="sr-only" htmlFor="iv-kind">Type</label>
      <select id="iv-kind" value={kind} onChange={(e) => setKind(e.target.value as "message" | "correct")} disabled={disabled}
        className="h-9 rounded-md border border-border-strong bg-surface px-2 text-sm">
        <option value="message">Message</option>
        <option value="correct">Standing order</option>
      </select>
      <label className="sr-only" htmlFor="iv-text">Message</label>
      <input id="iv-text" value={text} onChange={(e) => setText(e.target.value)} disabled={disabled}
        placeholder={disabled ? "Run has finished" : "Message the council, or give a standing order…"}
        className="h-9 flex-1 min-w-48 rounded-md border border-border-strong bg-surface px-3 text-sm" />
      <Button type="submit" variant="primary" icon={Send} loading={busy} disabled={disabled || !text.trim()}>Send</Button>
      <IconButton type="button" label="Halt all agents" icon={Hand} variant="danger-ghost" size="md" disabled={disabled} onClick={() => void haltAll()} />
    </form>
  );
}
