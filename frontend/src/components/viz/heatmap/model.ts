import type { RunState } from "@/stores/run";

import { deriveActivity, type Activity, type NodeRefLike } from "./comms";

export type Metric = "messages" | "tokens" | "cost" | "errors";

export interface HeatCell {
  messages: number;
  tokens: number;
  cost: number;
  errors: number;
  kinds: Record<string, number>;
}

export interface HeatRow { node: NodeRefLike; depth: number; total: number }

export interface HeatModel {
  rows: HeatRow[];
  cols: number;
  start: number;
  binMs: number;
  cells: HeatCell[][]; // [row][col]
}

const NICE_BINS = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 21600, 43200, 86400].map((s) => s * 1000);
export const MIN_COLS = 24;
export const MAX_COLS = 60;
export const LEVELS = 6; // 0 = none, 1..5

/** Smallest "nice" bin width that keeps the grid at ≤ MAX_COLS columns. */
export function pickBin(spanMs: number): number {
  for (const b of NICE_BINS) if (Math.ceil(spanMs / b) <= MAX_COLS) return b;
  return Math.ceil(spanMs / MAX_COLS);
}

const empty = (): HeatCell => ({ messages: 0, tokens: 0, cost: 0, errors: 0, kinds: {} });

export function buildHeat(run: RunState, acts: Activity[] = deriveActivity(run)): HeatModel {
  const times = acts.map((a) => a.t).filter((t) => t > 0);
  const start = times.length ? Math.min(...times) : 0;
  const end = times.length ? Math.max(...times) : 0;
  const binMs = pickBin(Math.max(1, end - start + 1));
  const cols = Math.max(MIN_COLS, Math.ceil((end - start + 1) / binMs));

  // Rows: council members in roster order first, then any other nodes (sub-agents, peers, user) as seen.
  const rowIndex = new Map<string, number>();
  const rows: HeatRow[] = [];
  const add = (node: NodeRefLike) => {
    if (rowIndex.has(node.id)) return;
    rowIndex.set(node.id, rows.length);
    rows.push({ node, depth: node.parent ? 1 : 0, total: 0 });
  };
  [...run.members].sort((a, b) => a.position - b.position).forEach((m) =>
    add({ kind: m.config.overseer ? "overseer" : "member", id: m.id, label: m.name, role: m.role }));
  const extra = acts.filter((a) => !rowIndex.has(a.node.id));
  // Keep sub-agents next to siblings of the same parent.
  extra.sort((a, b) => (a.node.parent ?? "").localeCompare(b.node.parent ?? "") || a.t - b.t);
  extra.forEach((a) => add(a.node));

  const cells = rows.map(() => Array.from({ length: cols }, empty));
  for (const a of acts) {
    const r = rowIndex.get(a.node.id);
    if (r === undefined) continue;
    const c = Math.min(cols - 1, Math.max(0, Math.floor((a.t - start) / binMs)));
    const cell = cells[r]![c]!;
    cell.messages += 1;
    cell.tokens += a.tokens;
    cell.cost += a.cost;
    if (a.error) cell.errors += 1;
    cell.kinds[a.kind] = (cell.kinds[a.kind] ?? 0) + 1;
    rows[r]!.total += 1;
  }
  return { rows, cols, start, binMs, cells };
}

export const metricValue = (c: HeatCell, m: Metric) => c[m];

/** Discrete level 0..5 relative to the grid's max value. */
export function level(v: number, max: number): number {
  if (v <= 0 || max <= 0) return 0;
  return Math.max(1, Math.min(LEVELS - 1, Math.ceil((v / max) * (LEVELS - 1))));
}

export function formatMetric(v: number, m: Metric): string {
  if (m === "cost") return v === 0 ? "$0" : v < 0.01 ? `$${v.toFixed(4)}` : `$${v.toFixed(2)}`;
  if (m === "tokens" && v >= 1000) return `${(v / 1000).toFixed(v >= 10000 ? 0 : 1)}k`;
  return String(Math.round(v));
}

/** Legend bins: the value range covered by each level (index 0 = none). */
export function legendRanges(max: number, m: Metric): string[] {
  const out = ["0"];
  if (m !== "cost" && max <= 500) {
    const lo: number[] = [];
    const hi: number[] = [];
    for (let v = 1; v <= max; v++) {
      const l = level(v, max);
      lo[l] ??= v;
      hi[l] = v;
    }
    for (let l = 1; l < LEVELS; l++) {
      const a = lo[l];
      const b = hi[l];
      out.push(a === undefined || b === undefined ? "–" : a === b ? formatMetric(a, m) : `${formatMetric(a, m)}–${formatMetric(b, m)}`);
    }
    return out;
  }
  for (let l = 1; l < LEVELS; l++) out.push(`≤ ${formatMetric((max * l) / (LEVELS - 1), m)}`);
  return out;
}

export function fmtClock(t: number): string {
  const d = new Date(t);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

export function fmtBin(binMs: number): string {
  const s = binMs / 1000;
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${s / 60}m`;
  if (s < 86400) return `${s / 3600}h`;
  return `${s / 86400}d`;
}
