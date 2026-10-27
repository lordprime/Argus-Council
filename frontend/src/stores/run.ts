// Per-run state built by reducing the event stream (ui-spec §6.5). The same reducer serves
// live streaming and replay, so the timeline renders identically in both.

import { create } from "zustand";

import type { Actor, Member, RunEvent } from "@/lib/types";

export type MemberStatus = "idle" | "thinking" | "tool" | "waiting" | "held" | "blocked" | "error";

export interface ToolCallItem {
  kind: "tool";
  id: string;
  seq: number;
  ts: string;
  actor: Actor;
  stepId?: string;
  thoughtEventId?: string;
  tool: string;
  source: string;
  args: unknown;
  status: "running" | "ok" | "error" | "blocked" | "timeout";
  preview?: string;
  output: string;
  durationMs?: number;
}

export interface InterceptInfo {
  id: string;
  by: "user" | "overseer";
  actor: Actor;
  reason: string;
  severity: string;
  action: string;
  note?: string;
  correction?: string;
  outcome: "pending" | "applied" | "rejected" | "overridden" | "superseded";
}

export interface ThoughtItem {
  kind: "thought";
  id: string;
  eventId?: string;
  seq: number;
  ts: string;
  actor: Actor;
  stepId?: string;
  text: string;
  streaming: boolean;
  tokensIn?: number;
  tokensOut?: number;
  costUsd?: number;
  latencyMs?: number;
  model?: string;
  finish?: string;
  intercepts: InterceptInfo[];
}

export interface ApprovalItem {
  kind: "approval";
  id: string;
  seq: number;
  ts: string;
  actor: Actor;
  summary: string;
  risk: string[];
  toolCallId?: string;
  status: "pending" | "approved" | "rejected" | "expired";
  reason?: string;
}

export interface VoteItem {
  kind: "vote";
  id: string;
  seq: number;
  ts: string;
  motion: string;
  options: { id: string; label: string }[];
  casts: { memberId: string; option: string; rationale?: string; actor: Actor }[];
  result?: string | null;
  tally?: Record<string, number>;
}

export interface SimpleItem {
  kind: "artifact" | "violation" | "citation" | "system" | "user" | "error" | "plan";
  id: string;
  seq: number;
  ts: string;
  actor: Actor;
  stepId?: string;
  data: Record<string, unknown>;
  type: string;
}

export type TimelineItem = ThoughtItem | ToolCallItem | ApprovalItem | VoteItem | SimpleItem;

export interface PlanStep {
  id: string;
  title: string;
  assignee_member_id?: string;
  status: string;
  depends_on?: string[];
  summary?: string;
}

export interface Budget {
  used: { usd: number; tokens_in: number; tokens_out: number; steps: number; runtime_s: number };
  limits: { usd: number | null; tokens: number | null; steps: number; runtime_s: number };
  by_member?: Record<string, { usd: number; tokens: number }>;
}

export interface Source {
  source_id: string;
  n: number;
  url: string;
  title: string;
  retrieved_at: string;
  snippet?: string;
  fetchedBy?: Actor;
}

export interface RunState {
  runId: string;
  lastSeq: number;
  status: string;
  statusReason?: string;
  plan: { version: number; steps: PlanStep[] } | null;
  members: Member[];
  memberStatus: Record<string, { status: MemberStatus; detail?: string }>;
  order: string[];
  items: Record<string, TimelineItem>;
  budget: Budget | null;
  sources: Source[];
  artifacts: Record<string, { path: string; version: number; kind: string; mime: string; size: number }>;
  exports: Record<string, { format: string; status: string; path?: string; error?: string }>;
  connection: "connecting" | "live" | "reconnecting" | "closed" | "lost";
  reconnectAttempt: number;
}

export function emptyRun(runId: string, members: Member[] = []): RunState {
  return {
    runId,
    lastSeq: 0,
    status: "queued",
    plan: null,
    members,
    memberStatus: {},
    order: [],
    items: {},
    budget: null,
    sources: [],
    artifacts: {},
    exports: {},
    connection: "connecting",
    reconnectAttempt: 0,
  };
}

const TERMINAL = new Set(["completed", "failed", "cancelled", "halted"]);
export const isTerminal = (status: string) => TERMINAL.has(status);

function push(s: RunState, item: TimelineItem) {
  if (!s.items[item.id]) s.order.push(item.id);
  s.items[item.id] = item;
}

function findThoughtByEvent(s: RunState, eventId?: string): ThoughtItem | undefined {
  if (!eventId) return undefined;
  for (let i = s.order.length - 1; i >= 0; i--) {
    const it = s.items[s.order[i]!];
    if (it?.kind === "thought" && it.eventId === eventId) return it;
  }
  return undefined;
}

/** Apply one event to a draft state (mutates; callers clone first). */
export function applyEvent(s: RunState, ev: RunEvent): void {
  if (!ev.ephemeral) {
    if (ev.seq <= s.lastSeq) return; // duplicate after reconnect
    s.lastSeq = ev.seq;
  }
  const d = ev.data as Record<string, any>; // eslint-disable-line @typescript-eslint/no-explicit-any
  const base = { seq: ev.seq, ts: ev.ts, actor: ev.actor, stepId: ev.step_id };
  switch (ev.type) {
    case "run.status":
      s.status = d.status;
      s.statusReason = d.reason;
      push(s, { kind: "system", id: ev.id, ...base, type: ev.type, data: d });
      break;
    case "run.plan":
      s.plan = { version: d.version, steps: d.steps };
      push(s, { kind: "plan", id: ev.id, ...base, type: ev.type, data: d });
      break;
    case "step.status":
      if (s.plan) {
        s.plan = {
          ...s.plan,
          steps: s.plan.steps.map((st) => (st.id === d.step_id ? { ...st, status: d.status, summary: d.summary } : st)),
        };
      }
      break;
    case "member.status":
      s.memberStatus = { ...s.memberStatus, [d.member_id]: { status: d.status, detail: d.detail } };
      break;
    case "thought.started":
      push(s, { kind: "thought", id: d.thought_id, eventId: undefined, ...base, text: "", streaming: true, intercepts: [] });
      break;
    case "thought.delta": {
      const t = s.items[d.thought_id];
      if (t?.kind === "thought") s.items[d.thought_id] = { ...t, text: t.text + d.text };
      break;
    }
    case "thought.completed": {
      const t = s.items[d.thought_id];
      const done: ThoughtItem = {
        ...(t?.kind === "thought" ? t : { kind: "thought", id: d.thought_id, ...base, intercepts: [] as InterceptInfo[] }),
        kind: "thought",
        eventId: ev.id,
        text: d.text ?? (t?.kind === "thought" ? t.text : ""),
        streaming: false,
        tokensIn: d.tokens?.in,
        tokensOut: d.tokens?.out,
        costUsd: d.cost_usd,
        latencyMs: d.latency_ms,
        model: d.model,
        finish: d.finish,
      } as ThoughtItem;
      push(s, done);
      break;
    }
    case "intercept.raised": {
      const info: InterceptInfo = {
        id: d.intercept_id, by: d.by, actor: ev.actor, reason: d.reason, severity: d.severity, action: d.action,
        note: d.note, outcome: "pending",
      };
      const thought = (d.target?.thought_id && s.items[d.target.thought_id]) || findThoughtByEvent(s, ev.causation_id);
      if (thought?.kind === "thought") {
        s.items[thought.id] = { ...thought, intercepts: [...thought.intercepts, info] };
      } else {
        push(s, { kind: "system", id: ev.id, ...base, type: ev.type, data: d });
      }
      break;
    }
    case "correction.issued": {
      let attached = false;
      for (const id of s.order) {
        const it = s.items[id];
        if (it?.kind !== "thought") continue;
        const idx = it.intercepts.findIndex((x) => x.id === d.intercept_id);
        if (idx >= 0) {
          const intercepts = [...it.intercepts];
          intercepts[idx] = { ...intercepts[idx]!, correction: d.text };
          s.items[id] = { ...it, intercepts };
          attached = true;
          break;
        }
      }
      if (!attached) push(s, { kind: "user", id: ev.id, ...base, type: ev.type, data: d });
      break;
    }
    case "correction.resolved":
      push(s, { kind: "system", id: ev.id, ...base, type: ev.type, data: d });
      break;
    case "tool.call.started":
      push(s, {
        kind: "tool", id: d.tool_call_id, ...base, thoughtEventId: ev.causation_id, tool: d.tool, source: d.source,
        args: d.args, status: "running", output: "",
      });
      break;
    case "tool.call.output": {
      const t = s.items[d.tool_call_id];
      if (t?.kind === "tool") s.items[d.tool_call_id] = { ...t, output: (t.output + d.chunk).slice(-20000) };
      break;
    }
    case "tool.call.completed": {
      const t = s.items[d.tool_call_id];
      if (t?.kind === "tool") {
        s.items[d.tool_call_id] = { ...t, status: d.status, preview: d.result_preview, durationMs: d.duration_ms };
      }
      break;
    }
    case "approval.requested":
      push(s, {
        kind: "approval", id: d.approval_id, ...base, summary: d.action_summary, risk: d.risk ?? [],
        toolCallId: d.tool_call_id, status: "pending",
      });
      break;
    case "approval.resolved": {
      const a = s.items[d.approval_id];
      if (a?.kind === "approval") s.items[d.approval_id] = { ...a, status: d.decision, reason: d.reason };
      break;
    }
    case "vote.opened":
      push(s, { kind: "vote", id: d.vote_id, seq: ev.seq, ts: ev.ts, motion: d.motion, options: d.options, casts: [] });
      break;
    case "vote.cast": {
      const v = s.items[d.vote_id];
      if (v?.kind === "vote") {
        s.items[d.vote_id] = {
          ...v, casts: [...v.casts, { memberId: d.member_id, option: d.option_id, rationale: d.rationale, actor: ev.actor }],
        };
      }
      break;
    }
    case "vote.closed": {
      const v = s.items[d.vote_id];
      if (v?.kind === "vote") s.items[d.vote_id] = { ...v, result: d.result_option_id, tally: d.tally };
      break;
    }
    case "artifact.upserted":
      s.artifacts = { ...s.artifacts, [d.path]: { path: d.path, version: d.version, kind: d.kind, mime: d.mime, size: d.size } };
      push(s, { kind: "artifact", id: ev.id, ...base, type: ev.type, data: d });
      break;
    case "artifact.deleted": {
      const next = { ...s.artifacts };
      delete next[d.path];
      s.artifacts = next;
      push(s, { kind: "artifact", id: ev.id, ...base, type: ev.type, data: d });
      break;
    }
    case "export.status":
      s.exports = { ...s.exports, [d.format]: { format: d.format, status: d.status, path: d.path, error: d.error } };
      break;
    case "budget.update":
      s.budget = d as Budget;
      break;
    case "citation.added":
      if (!s.sources.some((x) => x.url === d.url)) s.sources = [...s.sources, { ...(d as Source), fetchedBy: ev.actor }];
      push(s, { kind: "citation", id: ev.id, ...base, type: ev.type, data: d });
      break;
    case "guardrail.violation":
      push(s, { kind: "violation", id: ev.id, ...base, type: ev.type, data: d });
      break;
    case "user.message":
      push(s, { kind: "user", id: ev.id, ...base, type: ev.type, data: d });
      break;
    case "error":
      push(s, { kind: "error", id: ev.id, ...base, type: ev.type, data: d });
      break;
    default:
      if (!ev.ephemeral) push(s, { kind: "system", id: ev.id, ...base, type: ev.type, data: d });
  }
}

export function reduceAll(state: RunState, events: RunEvent[]): RunState {
  const s: RunState = { ...state, order: [...state.order], items: { ...state.items } };
  for (const ev of events) applyEvent(s, ev);
  return s;
}

interface RunsStore {
  runs: Record<string, RunState>;
  init: (runId: string, members: Member[]) => void;
  apply: (runId: string, events: RunEvent[]) => void;
  setConnection: (runId: string, connection: RunState["connection"], attempt?: number) => void;
  /** Optimistic local update, e.g. marking an approval as decided before the event arrives. */
  patchItem: (runId: string, id: string, patch: Partial<TimelineItem>) => void;
  setMemberStatus: (runId: string, memberId: string, status: MemberStatus) => void;
  drop: (runId: string) => void;
}

export const useRuns = create<RunsStore>((set) => ({
  runs: {},
  init: (runId, members) =>
    set((st) => (st.runs[runId] ? st : { runs: { ...st.runs, [runId]: emptyRun(runId, members) } })),
  apply: (runId, events) =>
    set((st) => {
      const cur = st.runs[runId] ?? emptyRun(runId);
      return { runs: { ...st.runs, [runId]: reduceAll(cur, events) } };
    }),
  setConnection: (runId, connection, attempt = 0) =>
    set((st) => {
      const cur = st.runs[runId];
      if (!cur) return st;
      return { runs: { ...st.runs, [runId]: { ...cur, connection, reconnectAttempt: attempt } } };
    }),
  patchItem: (runId, id, patch) =>
    set((st) => {
      const cur = st.runs[runId];
      const item = cur?.items[id];
      if (!cur || !item) return st;
      return { runs: { ...st.runs, [runId]: { ...cur, items: { ...cur.items, [id]: { ...item, ...patch } as TimelineItem } } } };
    }),
  setMemberStatus: (runId, memberId, status) =>
    set((st) => {
      const cur = st.runs[runId];
      if (!cur) return st;
      return { runs: { ...st.runs, [runId]: { ...cur, memberStatus: { ...cur.memberStatus, [memberId]: { status } } } } };
    }),
  drop: (runId) =>
    set((st) => {
      const next = { ...st.runs };
      delete next[runId];
      return { runs: next };
    }),
}));

export function useRun<T>(runId: string, select: (s: RunState) => T): T {
  return useRuns((st) => select(st.runs[runId] ?? emptyRun(runId)));
}
