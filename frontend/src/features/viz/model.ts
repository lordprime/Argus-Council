// Pure selectors that turn RunState into view models for the live visualisations.
// Time is derived from event timestamps (never the wall clock) so live and replay render alike.

import type { PlanStep, RunState } from "@/stores/run";

import { COUNCIL_NODE, USER_NODE, actorRef, memberRef } from "./comms";
import type { CommEvent, CommGraph, CommKind, NodeRef } from "./types";

const ms = (ts: string | undefined) => (ts ? Date.parse(ts) : NaN);

/** Latest timestamp seen in the run (comms or items), in epoch ms; NaN when empty. */
export function runNow(run: RunState): number {
  let now = NaN;
  const bump = (t: number) => {
    if (!Number.isNaN(t) && (Number.isNaN(now) || t > now)) now = t;
  };
  const last = run.comms[run.comms.length - 1];
  if (last) bump(ms(last.ts));
  const lastItem = run.items[run.order[run.order.length - 1] ?? ""];
  if (lastItem) {
    bump(ms(lastItem.ts));
    if ("endTs" in lastItem) bump(ms(lastItem.endTs));
  }
  return now;
}

/* ------------------------------------------------------------------ graph */

export interface BuildGraphOptions {
  /** When set, only comms within this window (ending at the latest event) contribute edges. */
  windowMs?: number;
  /** A node is "active" if it sent/received within this window (default 10 s) or its member is busy. */
  activeMs?: number;
}

const BUSY = new Set(["thinking", "tool", "waiting", "held"]);

export function buildGraph(run: RunState, opts: BuildGraphOptions = {}): CommGraph {
  const now = runNow(run);
  const activeMs = opts.activeMs ?? 10_000;
  const nodes = new Map<string, CommGraph["nodes"][number]>();
  const lastSeen = new Map<string, number>();
  const ensure = (ref: NodeRef) => {
    let n = nodes.get(ref.id);
    if (!n) {
      n = { ...ref, active: false, msgs: 0 };
      nodes.set(ref.id, n);
    }
    return n;
  };

  ensure(USER_NODE);
  for (const m of run.members) if (m.enabled !== false) ensure(memberRef(m));

  const edges = new Map<string, CommGraph["edges"][number]>();
  const from = opts.windowMs != null && !Number.isNaN(now) ? now - opts.windowMs : -Infinity;
  for (const c of run.comms) {
    const a = ensure(c.from);
    const b = ensure(c.to);
    const t = ms(c.ts);
    for (const n of [a, b]) {
      n.msgs += 1;
      if (!Number.isNaN(t)) lastSeen.set(n.id, Math.max(lastSeen.get(n.id) ?? -Infinity, t));
    }
    if (c.status && (c.to.kind === "tool" || c.to.kind === "mcp" || c.to.kind === "remote" || c.to.kind === "subcouncil")) b.status = c.status;
    if (c.status && (c.from.kind === "tool" || c.from.kind === "mcp" || c.from.kind === "remote" || c.from.kind === "subcouncil")) a.status = c.status;
    if (!Number.isNaN(t) && t < from) continue;
    const key = `${c.from.id}->${c.to.id}:${c.kind}`;
    const e = edges.get(key);
    if (e) {
      e.count += 1;
      e.lastTs = c.ts;
    } else {
      edges.set(key, { id: key, from: c.from.id, to: c.to.id, kind: c.kind, count: 1, lastTs: c.ts });
    }
  }

  for (const n of nodes.values()) {
    const ms_ = run.memberStatus[n.id];
    if (ms_) n.status = ms_.status;
    const sc = run.subcouncils[n.id];
    if (sc) n.status = sc.status;
    const seen = lastSeen.get(n.id);
    n.active =
      (ms_ ? BUSY.has(ms_.status) : false) ||
      (sc ? sc.status === "running" : false) ||
      (seen != null && !Number.isNaN(now) && now - seen <= activeMs);
  }
  // Council hub only when something talks to it.
  if (nodes.get(COUNCIL_NODE.id)?.msgs === 0) nodes.delete(COUNCIL_NODE.id);
  return { nodes: [...nodes.values()], edges: [...edges.values()] };
}

/* --------------------------------------------------------------- timeline */

export type SpanKind = "thinking" | "tool" | "waiting" | "subcouncil";

export interface ActivitySpan {
  id: string;
  kind: SpanKind;
  /** epoch ms */
  start: number;
  /** epoch ms; undefined while still open */
  end?: number;
  label: string;
  status?: string;
  /** timeline item id (thought/tool/approval) or sub-council id */
  itemId: string;
}

export interface TimelineRow {
  node: NodeRef;
  spans: ActivitySpan[];
}

export interface MemberTimeline {
  /** epoch ms of first activity (NaN when empty) */
  start: number;
  /** epoch ms of latest activity */
  end: number;
  rows: TimelineRow[];
}

/** Per-node activity intervals for a Gantt chart: thinking, tool calls, approval waits, sub-councils. */
export function memberTimeline(run: RunState): MemberTimeline {
  const rows = new Map<string, TimelineRow>();
  const row = (ref: NodeRef) => {
    let r = rows.get(ref.id);
    if (!r) {
      r = { node: ref, spans: [] };
      rows.set(ref.id, r);
    }
    return r;
  };
  for (const m of run.members) if (m.enabled !== false) row(memberRef(m));

  let start = NaN;
  let end = NaN;
  const see = (t: number | undefined) => {
    if (t == null || Number.isNaN(t)) return;
    if (Number.isNaN(start) || t < start) start = t;
    if (Number.isNaN(end) || t > end) end = t;
  };

  for (const id of run.order) {
    const it = run.items[id];
    if (!it) continue;
    if (it.kind === "thought") {
      const s = ms(it.ts);
      const e = it.endTs ? ms(it.endTs) : !it.streaming && it.latencyMs != null ? s + it.latencyMs : undefined;
      row(actorRef(run, it.actor)).spans.push({
        id: it.id, kind: "thinking", start: s, end: e, label: it.model ?? "thinking",
        status: it.streaming ? "running" : it.finish ?? "done", itemId: it.id,
      });
      see(s);
      see(e);
    } else if (it.kind === "tool") {
      if (it.tool === "subcouncil") continue; // drawn from the sub-council registry
      const s = ms(it.ts);
      const e = it.endTs ? ms(it.endTs) : it.durationMs != null ? s + it.durationMs : undefined;
      row(actorRef(run, it.actor)).spans.push({ id: it.id, kind: "tool", start: s, end: it.status === "running" ? undefined : e ?? s, label: it.tool, status: it.status, itemId: it.id });
      see(s);
      see(e);
    } else if (it.kind === "approval") {
      const s = ms(it.ts);
      const e = it.resolvedTs ? ms(it.resolvedTs) : undefined;
      row(actorRef(run, it.actor)).spans.push({ id: it.id, kind: "waiting", start: s, end: it.status === "pending" ? undefined : e ?? s, label: it.summary, status: it.status, itemId: it.id });
      see(s);
      see(e);
    }
  }

  for (const c of run.comms) {
    if (c.kind !== "subcouncil" || !c.childRunId || c.to.id !== c.childRunId) continue;
    const sc = run.subcouncils[c.childRunId];
    if (!sc) continue;
    const s = ms(sc.startedTs);
    const e = sc.endedTs ? ms(sc.endedTs) : undefined;
    row(c.from).spans.push({ id: `sc:${sc.id}`, kind: "subcouncil", start: s, end: e, label: sc.task, status: sc.status, itemId: sc.id });
    row({ kind: "subcouncil", id: sc.id, label: c.to.label, role: sc.mode }).spans.push({
      id: `scr:${sc.id}`, kind: "subcouncil", start: s, end: e, label: sc.task, status: sc.status, itemId: sc.id,
    });
    see(s);
    see(e);
  }
  for (const c of run.comms) see(ms(c.ts));

  for (const r of rows.values()) r.spans.sort((a, b) => a.start - b.start);
  return { start, end, rows: [...rows.values()] };
}

/* ---------------------------------------------------------------- heatmap */

export interface ActivityBins {
  /** epoch ms of bin 0 */
  start: number;
  binMs: number;
  /** number of bins */
  bins: number;
  nodes: NodeRef[];
  /** nodeId -> per-bin message counts (sent + received) */
  counts: Record<string, number[]>;
  /** nodeId -> per-bin per-kind counts, for tooltips */
  kinds: Record<string, Partial<Record<CommKind, number>>[]>;
  max: number;
}

/** Per-node, per-time-bin message counts for a heatmap. */
export function activityBins(run: RunState, binMs: number): ActivityBins {
  const size = Math.max(1, binMs);
  const times = run.comms.map((c) => ms(c.ts)).filter((t) => !Number.isNaN(t));
  const nodes = new Map<string, NodeRef>();
  for (const m of run.members) if (m.enabled !== false) nodes.set(m.id, memberRef(m));
  if (times.length === 0) {
    const list = [...nodes.values()];
    return { start: NaN, binMs: size, bins: 0, nodes: list, counts: Object.fromEntries(list.map((n) => [n.id, []])), kinds: Object.fromEntries(list.map((n) => [n.id, []])), max: 0 };
  }
  const start = Math.floor(Math.min(...times) / size) * size;
  const bins = Math.floor((Math.max(...times) - start) / size) + 1;
  const counts: Record<string, number[]> = {};
  const kinds: Record<string, Partial<Record<CommKind, number>>[]> = {};
  let max = 0;
  const bump = (ref: NodeRef, bin: number, kind: CommKind) => {
    if (!nodes.has(ref.id)) nodes.set(ref.id, ref);
    const row = (counts[ref.id] ??= new Array<number>(bins).fill(0));
    const krow = (kinds[ref.id] ??= Array.from({ length: bins }, () => ({})));
    row[bin] = (row[bin] ?? 0) + 1;
    const k = krow[bin]!;
    k[kind] = (k[kind] ?? 0) + 1;
    if (row[bin]! > max) max = row[bin]!;
  };
  for (const c of run.comms) {
    const t = ms(c.ts);
    if (Number.isNaN(t)) continue;
    const bin = Math.floor((t - start) / size);
    bump(c.from, bin, c.kind);
    if (c.to.id !== c.from.id) bump(c.to, bin, c.kind);
  }
  for (const id of nodes.keys()) {
    counts[id] ??= new Array<number>(bins).fill(0);
    kinds[id] ??= Array.from({ length: bins }, () => ({}));
  }
  return { start, binMs: size, bins, nodes: [...nodes.values()], counts, kinds, max };
}

/* ------------------------------------------------------------------ route */

export type PinKind = "intercept" | "correction" | "approval" | "vote" | "subcouncil" | "protocol";

export interface RoutePin {
  id: string;
  kind: PinKind;
  seq: number;
  ts: string;
  label?: string;
  status?: string;
  from: NodeRef;
  to: NodeRef;
  itemId?: string;
  childRunId?: string;
}

export interface RouteStep extends PlanStep {
  index: number;
  assignee?: NodeRef;
  pins: RoutePin[];
}

export interface RouteMilestones {
  steps: RouteStep[];
  /** pins not tied to a known plan step (e.g. emitted before the plan) */
  loose: RoutePin[];
  /** pins in event order, across all steps */
  all: RoutePin[];
}

const PIN_KINDS = new Set<CommKind>(["intercept", "correction", "approval", "vote", "subcouncil", "protocol"]);

function stepOfItem(run: RunState, itemId?: string): string | undefined {
  if (!itemId) return undefined;
  const it = run.items[itemId];
  return it && "stepId" in it ? it.stepId : undefined;
}

/** Plan steps in order with status, each carrying the notable exchanges that happened under it. */
export function routeMilestones(run: RunState): RouteMilestones {
  const steps: RouteStep[] = (run.plan?.steps ?? []).map((st, index) => {
    const m = run.members.find((x) => x.id === st.assignee_member_id);
    return { ...st, index, assignee: m ? memberRef(m) : undefined, pins: [] };
  });
  const byId = new Map(steps.map((s) => [s.id, s]));
  const loose: RoutePin[] = [];
  const all: RoutePin[] = [];
  // Sub-council internals and approval resolutions fold into their parent pins.
  for (const c of run.comms as CommEvent[]) {
    if (!PIN_KINDS.has(c.kind)) continue;
    if (c.childRunId && c.kind !== "subcouncil") continue;
    if (c.kind === "subcouncil" && c.to.id !== c.childRunId) continue;
    if (c.kind === "approval" && c.from.kind === "user") continue;
    if (c.kind === "subcouncil" && c.from.kind !== "member" && c.from.kind !== "overseer" && c.from.kind !== "system") continue;
    let status = c.status;
    if (c.kind === "subcouncil" && c.childRunId) status = run.subcouncils[c.childRunId]?.status ?? status;
    if (c.kind === "approval" && c.itemId) {
      const a = run.items[c.itemId];
      if (a?.kind === "approval") status = a.status;
    }
    const pin: RoutePin = {
      id: c.id, kind: c.kind as PinKind, seq: c.seq, ts: c.ts, label: c.label, status, from: c.from, to: c.to,
      itemId: c.itemId, childRunId: c.childRunId,
    };
    all.push(pin);
    const stepId = c.stepId ?? stepOfItem(run, c.itemId);
    const step = stepId ? byId.get(stepId) : undefined;
    (step ? step.pins : loose).push(pin);
  }
  return { steps, loose, all };
}
