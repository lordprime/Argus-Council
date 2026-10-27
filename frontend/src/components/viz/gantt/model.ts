// Pure derivation of Gantt rows/segments from a RunState. Kept free of React so it can be
// unit-tested and so the chart does not depend on the viz model being present.

import type { SubCouncilInfo } from "@/features/viz/types";
import type { Actor } from "@/lib/types";
import { isTerminal, type RunState, type TimelineItem } from "@/stores/run";

export type SegKind = "think" | "tool" | "wait" | "step" | "span";

export interface Seg {
  id: string;
  kind: SegKind;
  start: number;
  end: number;
  /** Still running at the "now" edge. */
  open: boolean;
  itemId?: string;
  nodeId?: string;
  title: string;
  who: string;
  role?: string;
  status?: string;
  tokensIn?: number;
  tokensOut?: number;
  costUsd?: number;
}

export type RowKind = "step" | "agent" | "group" | "submember";

export interface Row {
  id: string;
  kind: RowKind;
  label: string;
  role?: string;
  overseer?: boolean;
  status: string;
  nodeId?: string;
  start?: number;
  end?: number;
  costUsd?: number;
  segs: Seg[];
  deps: string[];
  /** Sub-council id for group and submember rows. */
  groupId?: string;
}

export interface GanttModel {
  t0: number;
  tEnd: number;
  live: boolean;
  steps: Row[];
  agents: Row[];
  groups: { row: Row; members: Row[] }[];
}

const DEFAULT_GAP = 1000;
type ActorItem = TimelineItem & { actor: Actor };
const hasActor = (it: TimelineItem): it is ActorItem => "actor" in it && !!(it as { actor?: Actor }).actor;
const ms = (iso: string | undefined) => {
  const t = iso ? Date.parse(iso) : NaN;
  return Number.isFinite(t) ? t : NaN;
};

export function actorKey(a: Actor | undefined): string | null {
  if (!a || (a.kind !== "agent" && a.kind !== "remote")) return null;
  return a.member_id ?? (a.name ? `name:${a.name}` : a.role ? `role:${a.role}` : null);
}

/** Normalise run.subcouncils (array or record) without depending on its exact declared type. */
export function readSubcouncils(run: RunState): SubCouncilInfo[] {
  const raw = (run as unknown as { subcouncils?: unknown }).subcouncils;
  const list = Array.isArray(raw) ? raw : raw && typeof raw === "object" ? Object.values(raw) : [];
  return list.filter((s): s is SubCouncilInfo =>
    !!s && typeof s === "object" && typeof (s as SubCouncilInfo).id === "string" && typeof (s as SubCouncilInfo).startedTs === "string");
}

const DONE = new Set(["completed", "done", "ok", "succeeded", "skipped"]);
const ACTIVE = new Set(["running", "in_progress", "active", "working", "started"]);

export function buildGantt(run: RunState, now: number): GanttModel {
  const live = !isTerminal(run.status);
  const items = run.order.map((id) => run.items[id]).filter((x): x is TimelineItem => !!x);
  const subs = readSubcouncils(run);

  let t0 = Infinity;
  for (const it of items) { const t = ms(it.ts); if (t < t0) t0 = t; }
  for (const s of subs) { const t = ms(s.startedTs); if (t < t0) t0 = t; }
  if (!Number.isFinite(t0)) t0 = now;
  let lastEvent = t0;
  for (const it of items) { const t = ms(it.ts); if (t > lastEvent) lastEvent = t; }
  const edge = live ? Math.max(now, lastEvent) : lastEvent;

  const members = [...run.members].sort((a, b) => a.position - b.position);
  const memberById = new Map(members.map((m) => [m.id, m]));
  const whoOf = (a: Actor) => (a.member_id && memberById.get(a.member_id)?.name) || a.name || a.role || "agent";
  const roleOf = (a: Actor) => (a.member_id && memberById.get(a.member_id)?.role) || a.role;

  // Per-actor event times so un-timed segments can run until the actor's next event.
  const byActor = new Map<string, ActorItem[]>();
  for (const it of items) {
    if (!hasActor(it)) continue;
    const k = actorKey(it.actor);
    if (!k) continue;
    const arr = byActor.get(k) ?? [];
    arr.push(it);
    byActor.set(k, arr);
  }

  const segByItem = new Map<string, Seg>();
  const agentSegs = new Map<string, Seg[]>();
  for (const [k, list] of byActor) {
    list.sort((a, b) => ms(a.ts) - ms(b.ts));
    const segs: Seg[] = [];
    list.forEach((it, i) => {
      const start = ms(it.ts);
      if (!Number.isFinite(start)) return;
      const next = list.slice(i + 1).map((n) => ms(n.ts)).find((t) => t > start);
      const fallback = Math.min(next ?? start + DEFAULT_GAP, start + 60_000);
      const common = { who: whoOf(it.actor), role: roleOf(it.actor), itemId: it.id };
      let seg: Seg | null = null;
      if (it.kind === "thought") {
        const open = it.streaming && live;
        const end = it.latencyMs != null ? start + it.latencyMs : open ? edge : it.streaming ? (next ?? start) : fallback;
        seg = { ...common, id: it.id, kind: "think", start, end, open, title: it.model ? `Thinking · ${it.model}` : "Thinking",
          status: it.streaming ? "streaming" : it.finish ?? "done", tokensIn: it.tokensIn, tokensOut: it.tokensOut, costUsd: it.costUsd };
      } else if (it.kind === "tool") {
        const open = it.status === "running" && live;
        const end = it.durationMs != null ? start + it.durationMs : open ? edge : it.status === "running" ? (next ?? start) : fallback;
        seg = { ...common, id: it.id, kind: "tool", start, end, open, title: `Tool · ${it.tool}`, status: it.status };
      } else if (it.kind === "approval") {
        const open = it.status === "pending" && live;
        const end = open ? edge : next ?? start + DEFAULT_GAP;
        seg = { ...common, id: it.id, kind: "wait", start, end, open, title: `Awaiting approval · ${it.summary}`, status: it.status };
      }
      if (seg) {
        seg.end = Math.max(seg.end, seg.start);
        segs.push(seg);
        segByItem.set(it.id, seg);
      }
    });
    agentSegs.set(k, segs);
  }

  // Members currently held/waiting get a hatched segment from their last activity to now.
  if (live) {
    for (const [mid, st] of Object.entries(run.memberStatus)) {
      if (!["held", "waiting", "blocked"].includes(st.status)) continue;
      const segs = agentSegs.get(mid) ?? [];
      if (segs.some((s) => s.kind === "wait" && s.open)) continue;
      const from = segs.reduce((m, s) => Math.max(m, s.end), lastEvent);
      const m = memberById.get(mid);
      segs.push({ id: `${mid}:${st.status}`, kind: "wait", start: Math.min(from, edge), end: edge, open: true, nodeId: mid,
        title: st.detail ? `${cap(st.status)} · ${st.detail}` : cap(st.status), who: m?.name ?? mid, role: m?.role, status: st.status });
      agentSegs.set(mid, segs);
    }
  }

  const span = (segs: Seg[]) => segs.length
    ? { start: Math.min(...segs.map((s) => s.start)), end: Math.max(...segs.map((s) => s.end)) }
    : { start: undefined, end: undefined };
  const cost = (segs: Seg[]) => segs.reduce((n, s) => n + (s.costUsd ?? 0), 0);

  // Agent rows: roster order, then any actors seen that are not in the roster.
  const agents: Row[] = [];
  const seen = new Set<string>();
  for (const m of members) {
    const segs = agentSegs.get(m.id) ?? [];
    seen.add(m.id);
    agents.push({ id: `agent:${m.id}`, kind: "agent", label: m.name, role: m.role, overseer: !!m.config?.overseer,
      status: run.memberStatus[m.id]?.status ?? (segs.length ? "idle" : "not started"), nodeId: m.id,
      ...span(segs), costUsd: cost(segs), segs, deps: [] });
  }
  for (const [k, segs] of agentSegs) {
    if (seen.has(k)) continue;
    const first = byActor.get(k)?.[0]?.actor;
    agents.push({ id: `agent:${k}`, kind: "agent", label: first ? whoOf(first) : k, role: first ? roleOf(first) : undefined,
      overseer: !!first?.overseer, status: run.memberStatus[k]?.status ?? "idle", nodeId: first?.member_id ?? k,
      ...span(segs), costUsd: cost(segs), segs, deps: [] });
  }

  // Plan steps: from first activity to completion, coloured by the assignee's role.
  const steps: Row[] = (run.plan?.steps ?? []).map((st) => {
    const its = items.filter((it) => "stepId" in it && it.stepId === st.id);
    const segs = its.map((it) => segByItem.get(it.id)).filter((s): s is Seg => !!s);
    const times = its.map((it) => ms(it.ts)).filter(Number.isFinite);
    const assignee = st.assignee_member_id ? memberById.get(st.assignee_member_id) : undefined;
    let start = segs.length ? Math.min(...segs.map((s) => s.start)) : times.length ? Math.min(...times) : undefined;
    let end = segs.length ? Math.max(...segs.map((s) => s.end)) : times.length ? Math.max(...times) : undefined;
    const active = ACTIVE.has(st.status) && live;
    if (start == null && active) start = edge;
    if (start != null && active) end = edge;
    if (start != null && end != null && end - start < 250 && !DONE.has(st.status)) end = start + 250;
    const c = cost(segs);
    const bar: Seg[] = start != null && end != null ? [{
      id: `step:${st.id}`, kind: "step", start, end, open: active, itemId: its[0]?.id, nodeId: st.assignee_member_id,
      title: st.title, who: assignee?.name ?? "Unassigned", role: assignee?.role, status: st.status, costUsd: c || undefined,
      tokensIn: sum(segs, "tokensIn"), tokensOut: sum(segs, "tokensOut"),
    }] : [];
    return { id: `step:${st.id}`, kind: "step", label: st.title, role: assignee?.role, status: st.status,
      nodeId: st.assignee_member_id, start, end, costUsd: c, segs: bar, deps: (st.depends_on ?? []).map((d) => `step:${d}`) };
  });

  // Sub-councils: a span per council, and per member.
  const groups = subs.map((sc) => {
    const start = ms(sc.startedTs);
    const ended = ms(sc.endedTs);
    const open = !Number.isFinite(ended) && live && !isTerminal(sc.status);
    const end = Number.isFinite(ended) ? ended : open ? edge : Math.max(start, lastEvent);
    const row: Row = { id: `sub:${sc.id}`, kind: "group", label: sc.task || `Sub-council ${sc.id}`, status: sc.status,
      nodeId: sc.id, groupId: sc.id, start, end, segs: [{ id: `sub:${sc.id}`, kind: "span", start, end, open, nodeId: sc.id,
        title: `Sub-council · ${sc.mode} · depth ${sc.depth}`, who: sc.task, status: sc.status }], deps: [] };
    const mems: Row[] = (sc.members ?? []).map((m) => ({
      id: `sub:${sc.id}:${m.id}`, kind: "submember", label: m.name, role: m.role, overseer: m.overseer, status: sc.status,
      nodeId: m.id, groupId: sc.id, start, end, deps: [],
      segs: [{ id: `sub:${sc.id}:${m.id}`, kind: "span", start, end, open, nodeId: m.id, title: `Member of “${sc.task}”`,
        who: m.name, role: m.role, status: sc.status }],
    }));
    return { row, members: mems };
  });

  let tEnd = edge;
  for (const r of [...steps, ...agents, ...groups.flatMap((g) => [g.row, ...g.members])]) {
    for (const s of r.segs) if (s.end > tEnd) tEnd = s.end;
  }
  return { t0, tEnd, live, steps, agents, groups };
}

function sum(segs: Seg[], k: "tokensIn" | "tokensOut") {
  const n = segs.reduce((a, s) => a + (s[k] ?? 0), 0);
  return n || undefined;
}
const cap = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);

/** "850ms", "12.3s", "4m 05s", "1h 02m". */
export function fmtDur(d: number | undefined): string {
  if (d == null || !Number.isFinite(d)) return "—";
  if (d < 1000) return `${Math.max(0, Math.round(d))}ms`;
  const s = d / 1000;
  if (s < 60) return `${s < 10 ? s.toFixed(1) : Math.round(s)}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${String(Math.floor(s % 60)).padStart(2, "0")}s`;
  return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m`;
}

/** Offset label relative to run start: "+12s", "+1:05", "+1:02:03". */
export function fmtOffset(d: number): string {
  const s = Math.max(0, Math.round(d / 1000));
  if (s < 60) return `+${s}s`;
  const h = Math.floor(s / 3600);
  const mm = Math.floor((s % 3600) / 60);
  const ss = String(s % 60).padStart(2, "0");
  return h ? `+${h}:${String(mm).padStart(2, "0")}:${ss}` : `+${mm}:${ss}`;
}

const TICK_STEPS = [0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200].map((s) => s * 1000);
/** Pick a tick interval so labels are at least `minPx` apart. */
export function tickStep(pxPerMs: number, minPx = 72): number {
  return TICK_STEPS.find((t) => t * pxPerMs >= minPx) ?? TICK_STEPS[TICK_STEPS.length - 1]!;
}
