// Hand-rolled, token-coloured SVG charts for the insights dashboard. No chart library.
//
// Accessibility pattern shared by every chart: the drawing is an <svg role="img"> whose aria-label is a
// one-sentence summary; a transparent role="slider" overlay lets keyboard users step through the data
// points (arrow keys, Home/End) and drives the same tooltip as the pointer, announcing each point via
// aria-valuetext; and ChartCard offers a "View as table" toggle with the exact numbers.

import { useCallback, useId, useLayoutEffect, useRef, useState, type KeyboardEvent, type PointerEvent, type ReactNode } from "react";

import { Button, Card, CardTitle } from "@/components/ui";
import { cn } from "@/lib/cn";

export type Tone = "accent" | "info" | "success" | "warning" | "danger" | "neutral";
/**
 * Identity series: the accent, then neutral. Status hues (success, warning, danger, info) are kept for
 * status and never stand in for "series 3"; a chart with more identities is a bar list or a table.
 */
export const SERIES: Tone[] = ["accent", "neutral"];
export const seriesTone = (i: number): Tone => (i === 0 ? "accent" : "neutral");
export const color = (t: Tone, alpha = 1) => (alpha === 1 ? `rgb(var(--${t}))` : `rgb(var(--${t}) / ${alpha})`);

// ---------------------------------------------------------------- layout helpers
function useWidth<T extends HTMLElement>(fallback = 560) {
  const ref = useRef<T>(null);
  const [w, setW] = useState(fallback);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const measure = () => {
      if (el.clientWidth > 0) setW(el.clientWidth);
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, w] as const;
}

function niceMax(v: number) {
  if (v <= 0) return 1;
  const p = 10 ** Math.floor(Math.log10(v));
  const n = v / p;
  return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10) * p;
}

const clip = (s: string, max: number) => (s.length > max ? `${s.slice(0, Math.max(1, max - 1))}…` : s);

/** Index-stepping state shared by pointer and keyboard. */
function useExplorer(n: number, step2d = 1) {
  const [i, setI] = useState<number | null>(null);
  const onKeyDown = useCallback((e: KeyboardEvent) => {
    const move: Record<string, (c: number) => number> = {
      ArrowRight: (c) => c + 1, ArrowLeft: (c) => c - 1, ArrowDown: (c) => c + step2d, ArrowUp: (c) => c - step2d,
      Home: () => 0, End: () => n - 1,
    };
    const f = move[e.key];
    if (!f || n === 0) return;
    e.preventDefault();
    setI((c) => Math.min(n - 1, Math.max(0, f(c ?? 0))));
  }, [n, step2d]);
  return { i, setI, onKeyDown, onFocus: () => setI((c) => c ?? 0), onBlur: () => setI(null) };
}

function Tooltip({ x, y, width, children }: { x: number; y: number; width: number; children: ReactNode }) {
  const W = 168;
  const left = Math.min(Math.max(0, x - W / 2), Math.max(0, width - W));
  return (
    <div aria-hidden className="pointer-events-none absolute z-sticky rounded-lg border border-border bg-surface shadow-2 px-2.5 py-1.5 text-xs"
      style={{ left, top: Math.max(0, y - 8), width: W, transform: "translateY(-100%)" }}>
      {children}
    </div>
  );
}

function Explorer({ label, n, i, valueText, onKeyDown, onFocus, onBlur, onPointerMove, onPointerLeave }: {
  label: string; n: number; i: number | null; valueText: string;
} & Pick<ReturnType<typeof useExplorer>, "onKeyDown" | "onFocus" | "onBlur"> & {
  onPointerMove: (e: PointerEvent<HTMLDivElement>) => void; onPointerLeave: () => void;
}) {
  return (
    <div role="slider" tabIndex={n ? 0 : -1} aria-label={label} aria-valuemin={0} aria-valuemax={Math.max(0, n - 1)}
      aria-valuenow={i ?? 0} aria-valuetext={valueText}
      onKeyDown={onKeyDown} onFocus={onFocus} onBlur={onBlur} onPointerMove={onPointerMove} onPointerLeave={onPointerLeave}
      className="absolute inset-0 rounded-md focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2" />
  );
}

// ---------------------------------------------------------------- ChartCard
export interface TableData { columns: string[]; rows: (string | number)[][] }

export function ChartCard({ title, summary, table, actions, children, className }: {
  title: string; summary: string; table: TableData; actions?: ReactNode; children: ReactNode; className?: string;
}) {
  const [asTable, setAsTable] = useState(false);
  const id = useId();
  return (
    <Card className={cn("p-4 flex flex-col gap-3 min-w-0", className)} aria-labelledby={`${id}-t`}>
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0 flex-1">
          <CardTitle as="h2" id={`${id}-t`}>{title}</CardTitle>
          <p className="text-xs text-fg-subtle">{summary}</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {actions}
          <Button size="sm" variant="ghost" aria-pressed={asTable} aria-describedby={`${id}-t`} onClick={() => setAsTable((v) => !v)}>
            {asTable ? "View as chart" : "View as table"}
          </Button>
        </div>
      </div>
      {asTable ? (
        <div tabIndex={0} role="region" aria-label={`${title} data`} className="relative overflow-x-auto max-h-80 overflow-y-auto">
          <table className="w-full text-sm">
            <caption className="sr-only">{title}</caption>
            <thead className="bg-surface-2 text-left text-xs text-fg-subtle">
              <tr>{table.columns.map((c, ci) => <th key={c} scope="col" className={cn("px-2 py-1.5 font-medium", ci > 0 && "text-right")}>{c}</th>)}</tr>
            </thead>
            <tbody className="divide-y divide-border">
              {table.rows.length === 0 ? (
                <tr><td colSpan={table.columns.length} className="px-2 py-3 text-fg-subtle">No data in this window.</td></tr>
              ) : table.rows.map((r, ri) => (
                <tr key={ri}>{r.map((v, ci) => ci === 0
                  ? <th key={ci} scope="row" className="px-2 py-1.5 font-normal text-left break-all">{v}</th>
                  : <td key={ci} className="px-2 py-1.5 text-right tabular whitespace-nowrap">{v}</td>)}</tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : children}
    </Card>
  );
}

// ---------------------------------------------------------------- Sparkline
/** Decorative trend strip for KPI cards (the card's text carries the value). */
export function Sparkline({ values, tone = "accent", kind = "line" }: { values: number[]; tone?: Tone; kind?: "line" | "bars" }) {
  const W = 120, H = 32;
  const max = Math.max(...values, 0) || 1;
  const n = values.length;
  if (kind === "bars") {
    const bw = W / Math.max(1, n);
    return (
      <svg aria-hidden viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" className="w-full h-8">
        {values.map((v, i) => (
          <rect key={i} x={i * bw + bw * 0.15} width={bw * 0.7} y={H - Math.max(1.5, (v / max) * H)} height={Math.max(1.5, (v / max) * H)} rx={1}
            style={{ fill: color(tone, v ? 0.85 : 0.2) }} />
        ))}
      </svg>
    );
  }
  const pts = values.map((v, i) => `${n > 1 ? (i / (n - 1)) * W : W / 2},${H - 2 - (v / max) * (H - 4)}`);
  return (
    <svg aria-hidden viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" className="w-full h-8">
      {n > 1 && <polygon points={`0,${H} ${pts.join(" ")} ${W},${H}`} style={{ fill: color(tone, 0.12) }} />}
      <polyline points={pts.join(" ")} fill="none" vectorEffect="non-scaling-stroke" strokeWidth={1.75} strokeLinejoin="round" style={{ stroke: color(tone) }} />
    </svg>
  );
}

// ---------------------------------------------------------------- Horizontal bar chart
export interface BarDatum { label: string; value: number; display: string; detail?: string }

export function BarChart({ data, label, tone = "accent" }: { data: BarDatum[]; label: string; tone?: Tone }) {
  const [ref, w] = useWidth<HTMLDivElement>();
  const ex = useExplorer(data.length);
  // Narrow cards stack each label above its bar, so long names (model ids) are not cut short.
  const stacked = w < 440;
  const BAR = 10, GAP = 4, ROW = stacked ? 34 : 28;
  const labelW = stacked ? 0 : Math.min(170, Math.round(w * 0.38));
  const plotW = Math.max(40, w - labelW - 8);
  const barY = (y: number) => (stacked ? y + 20 : y + (ROW - BAR) / 2);
  const max = Math.max(...data.map((d) => d.value), 0) || 1;
  const H = data.length * (ROW + GAP);
  const active = ex.i != null ? data[ex.i] : undefined;
  const pick = (e: PointerEvent<HTMLDivElement>) => {
    const r = e.currentTarget.getBoundingClientRect();
    const idx = Math.floor((e.clientY - r.top) / (ROW + GAP));
    ex.setI(idx >= 0 && idx < data.length ? idx : null);
  };
  return (
    <div ref={ref} className="relative w-full">
      <svg role="img" aria-label={label} width={w} height={H} className="block max-w-full">
        {data.map((d, i) => {
          const y = i * (ROW + GAP);
          const valueW = d.display.length * 7 + 8;
          const bw = Math.max(3, (d.value / max) * (plotW - valueW));
          return (
            <g key={d.label}>
              <text x={0} y={stacked ? y + 8 : y + ROW / 2} dominantBaseline="central" className="text-xs" style={{ fill: "rgb(var(--text-muted))" }}>
                {clip(d.label, Math.floor((stacked ? w : labelW) / 6.6))}
              </text>
              <rect x={labelW} y={barY(y)} width={bw} height={BAR} rx={4}
                style={{ fill: color(tone, ex.i == null || ex.i === i ? 1 : 0.45), transition: "fill var(--dur-fast)" }} />
              <text x={labelW + bw + 6} y={barY(y) + BAR / 2} dominantBaseline="central"
                className="text-xs font-semibold tabular" style={{ fill: "rgb(var(--text))" }}>
                {d.display}
              </text>
            </g>
          );
        })}
      </svg>
      <Explorer label={`${label} — explore bars`} n={data.length} i={ex.i}
        valueText={active ? `${active.label}: ${active.display}${active.detail ? `, ${active.detail}` : ""}` : "Use arrow keys to read each bar"}
        onKeyDown={ex.onKeyDown} onFocus={ex.onFocus} onBlur={ex.onBlur} onPointerMove={pick} onPointerLeave={() => ex.setI(null)} />
      {active && ex.i != null && (
        <Tooltip x={labelW + Math.max(3, (active.value / max) * (plotW - 40)) / 2} y={ex.i * (ROW + GAP)} width={w}>
          <p className="font-semibold break-all">{active.label}</p>
          <p className="tabular">{active.display}</p>
          {active.detail && <p className="text-fg-subtle">{active.detail}</p>}
        </Tooltip>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- Area chart
export interface AreaPoint { label: string; value: number; detail?: string }

export function AreaChart({ points, label, format = String, tone = "accent", height = 200 }: {
  points: AreaPoint[]; label: string; format?: (v: number) => string; tone?: Tone; height?: number;
}) {
  const [ref, w] = useWidth<HTMLDivElement>();
  const gid = useId().replace(/[^\w-]/g, "");
  const ex = useExplorer(points.length);
  const L = 36, R = 8, T = 10, B = 22;
  const n = points.length;
  const max = niceMax(Math.max(...points.map((p) => p.value), 0));
  const x = (i: number) => L + (n > 1 ? (i / (n - 1)) * (w - L - R) : (w - L - R) / 2);
  const y = (v: number) => T + (1 - v / max) * (height - T - B);
  const line = points.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p.value).toFixed(1)}`).join(" ");
  const area = n ? `${line} L${x(n - 1).toFixed(1)},${y(0)} L${x(0).toFixed(1)},${y(0)} Z` : "";
  const ticks = [0, max / 2, max];
  const xLabels = n <= 1 ? [0] : Array.from(new Set([0, Math.round((n - 1) / 2), n - 1]));
  const active = ex.i != null ? points[ex.i] : undefined;
  const pick = (e: PointerEvent<HTMLDivElement>) => {
    const r = e.currentTarget.getBoundingClientRect();
    const rel = (e.clientX - r.left - L) / Math.max(1, w - L - R);
    ex.setI(n ? Math.min(n - 1, Math.max(0, Math.round(rel * (n - 1)))) : null);
  };
  return (
    <div ref={ref} className="relative w-full">
      <svg role="img" aria-label={label} width={w} height={height} className="block max-w-full">
        <defs>
          <linearGradient id={`g${gid}`} x1="0" x2="0" y1="0" y2="1">
            <stop offset="0%" style={{ stopColor: color(tone), stopOpacity: 0.28 }} />
            <stop offset="100%" style={{ stopColor: color(tone), stopOpacity: 0.02 }} />
          </linearGradient>
        </defs>
        {ticks.map((t) => (
          <g key={t}>
            <line x1={L} x2={w - R} y1={y(t)} y2={y(t)} style={{ stroke: "rgb(var(--border))" }} />
            <text x={L - 6} y={y(t)} textAnchor="end" dominantBaseline="central" className="text-2xs tabular" style={{ fill: "rgb(var(--text-subtle))" }}>
              {format(t)}
            </text>
          </g>
        ))}
        {xLabels.map((i) => points[i] && (
          <text key={i} x={x(i)} y={height - 6} textAnchor={i === 0 ? "start" : i === n - 1 ? "end" : "middle"} className="text-2xs"
            style={{ fill: "rgb(var(--text-subtle))" }}>{points[i].label}</text>
        ))}
        {n > 0 && <path d={area} style={{ fill: `url(#g${gid})` }} />}
        {n > 0 && <path d={line} fill="none" strokeWidth={2} strokeLinejoin="round" style={{ stroke: color(tone) }} />}
        {active && ex.i != null && (
          <g>
            <line x1={x(ex.i)} x2={x(ex.i)} y1={T} y2={y(0)} style={{ stroke: "rgb(var(--border-strong))" }} />
            <circle cx={x(ex.i)} cy={y(active.value)} r={4.5} strokeWidth={2} style={{ fill: "rgb(var(--surface))", stroke: color(tone) }} />
          </g>
        )}
      </svg>
      <Explorer label={`${label} — explore points`} n={n} i={ex.i}
        valueText={active ? `${active.label}: ${format(active.value)}${active.detail ? `, ${active.detail}` : ""}` : "Use arrow keys to read each point"}
        onKeyDown={ex.onKeyDown} onFocus={ex.onFocus} onBlur={ex.onBlur} onPointerMove={pick} onPointerLeave={() => ex.setI(null)} />
      {active && ex.i != null && (
        <Tooltip x={x(ex.i)} y={y(active.value)} width={w}>
          <p className="text-fg-subtle">{active.label}</p>
          <p className="font-semibold tabular">{format(active.value)}</p>
          {active.detail && <p className="text-fg-muted">{active.detail}</p>}
        </Tooltip>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- Stacked columns over time
export interface ColumnKey { key: string; label: string; tone: Tone }
export interface Column { label: string; values: Record<string, number>; detail?: string }

/**
 * Counts per period, stacked by kind (for example runs by outcome). Thin columns on solid hairline
 * gridlines, a 2px surface gap between stacked segments, a legend above, and the same pointer and
 * keyboard explorer as the other charts.
 */
export function StackedColumns({ columns, keys, label, height = 220, format = String }: {
  columns: Column[]; keys: ColumnKey[]; label: string; height?: number; format?: (v: number) => string;
}) {
  const [ref, w] = useWidth<HTMLDivElement>();
  const ex = useExplorer(columns.length);
  const L = 32, R = 4, T = 8, B = 22, SEP = 2;
  const n = columns.length;
  const totals = columns.map((c) => keys.reduce((s, k) => s + (c.values[k.key] ?? 0), 0));
  const max = niceMax(Math.max(...totals, 0));
  const slot = (w - L - R) / Math.max(1, n);
  const colW = Math.max(2, Math.min(28, slot * 0.62));
  const y = (v: number) => T + (1 - v / max) * (height - T - B);
  const ticks = [0, max / 2, max];
  const every = Math.max(1, Math.ceil(n / Math.max(1, Math.floor((w - L) / 64))));
  const active = ex.i != null ? columns[ex.i] : undefined;
  const pick = (e: PointerEvent<HTMLDivElement>) => {
    const r = e.currentTarget.getBoundingClientRect();
    const idx = Math.floor((e.clientX - r.left - L) / slot);
    ex.setI(idx >= 0 && idx < n ? idx : null);
  };
  const describe = (c: Column, t: number) =>
    `${c.label}: ${format(t)}${keys.map((k) => (c.values[k.key] ? `, ${c.values[k.key]} ${k.label.toLowerCase()}` : "")).join("")}`;
  return (
    <div className="flex flex-col gap-2">
      <ul className="flex flex-wrap gap-x-4 gap-y-1 text-xs" aria-label="Legend">
        {keys.map((k) => (
          <li key={k.key} className="inline-flex items-center gap-1.5 text-fg-muted">
            <span aria-hidden className="size-2.5 rounded-sm" style={{ background: color(k.tone) }} />{k.label}
          </li>
        ))}
      </ul>
      <div ref={ref} className="relative w-full">
        <svg role="img" aria-label={label} width={w} height={height} className="block max-w-full">
          {ticks.map((t) => (
            <g key={t}>
              <line x1={L} x2={w - R} y1={y(t)} y2={y(t)} style={{ stroke: "rgb(var(--border))" }} />
              <text x={L - 6} y={y(t)} textAnchor="end" dominantBaseline="central" className="text-2xs tabular" style={{ fill: "rgb(var(--text-subtle))" }}>
                {format(t)}
              </text>
            </g>
          ))}
          {active && ex.i != null && (
            <rect x={L + ex.i * slot} y={T} width={slot} height={height - T - B} rx={3} style={{ fill: "rgb(var(--surface-2))" }} />
          )}
          {columns.map((c, i) => {
            const x = L + i * slot + (slot - colW) / 2;
            let base = 0;
            return (
              <g key={i} style={{ opacity: ex.i == null || ex.i === i ? 1 : 0.5, transition: "opacity var(--dur-fast)" }}>
                {keys.map((k) => {
                  const v = c.values[k.key] ?? 0;
                  if (!v) return null;
                  const top = y(base + v), bottom = y(base);
                  const h = Math.max(1, bottom - top - (base > 0 ? SEP : 0));
                  base += v;
                  return <rect key={k.key} x={x} y={top} width={colW} height={h} rx={Math.min(3, colW / 3)} style={{ fill: color(k.tone) }} />;
                })}
              </g>
            );
          })}
          {columns.map((c, i) => i % every === 0 && (
            <text key={`l${i}`} x={L + i * slot + slot / 2} y={height - 6} textAnchor="middle" className="text-2xs"
              style={{ fill: "rgb(var(--text-subtle))" }}>{c.label}</text>
          ))}
        </svg>
        <Explorer label={`${label} — explore periods`} n={n} i={ex.i}
          valueText={active && ex.i != null ? describe(active, totals[ex.i] ?? 0) : "Use arrow keys to read each period"}
          onKeyDown={ex.onKeyDown} onFocus={ex.onFocus} onBlur={ex.onBlur} onPointerMove={pick} onPointerLeave={() => ex.setI(null)} />
        {active && ex.i != null && (
          <Tooltip x={L + ex.i * slot + slot / 2} y={y(totals[ex.i] ?? 0)} width={w}>
            <p className="text-fg-subtle">{active.label}</p>
            <p className="font-semibold tabular">{format(totals[ex.i] ?? 0)}</p>
            {keys.map((k) => (active.values[k.key] ?? 0) > 0 && (
              <p key={k.key} className="flex items-center gap-1.5 text-fg-muted">
                <span aria-hidden className="size-2 rounded-sm" style={{ background: color(k.tone) }} />
                {k.label} <span className="ml-auto tabular text-fg">{active.values[k.key]}</span>
              </p>
            ))}
            {active.detail && <p className="text-fg-subtle">{active.detail}</p>}
          </Tooltip>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- Inline meter
/** A rate against 100% as a thin bar on a same-ramp track (table cells, lists). Decorative: the text beside it carries the value. */
export function Meter({ value, tone }: { value: number; tone: Tone }) {
  return (
    <span aria-hidden className="relative inline-block h-1.5 w-16 shrink-0 overflow-hidden rounded-full" style={{ background: color(tone, 0.16) }}>
      <span className="absolute inset-y-0 left-0 rounded-full" style={{ width: `${Math.min(100, Math.max(value > 0 ? 4 : 0, value))}%`, background: color(tone) }} />
    </span>
  );
}

// ---------------------------------------------------------------- Stacked distribution bar
export interface Segment { label: string; value: number; tone: Tone }

export function StackedBar({ segments, label }: { segments: Segment[]; label: string }) {
  const total = segments.reduce((s, x) => s + x.value, 0);
  const clipId = `c${useId().replace(/[^\w-]/g, "")}`;
  let acc = 0;
  return (
    <div className="flex flex-col gap-2">
      <svg role="img" aria-label={label} width="100%" height={14} className="block">
        <defs><clipPath id={clipId}><rect x="0" y="0" width="100%" height="14" rx="7" /></clipPath></defs>
        <g clipPath={`url(#${clipId})`}>
          <rect x="0" y="0" width="100%" height="14" style={{ fill: "rgb(var(--surface-2))" }} />
          {total > 0 && segments.map((s) => {
            const x = (acc / total) * 100;
            acc += s.value;
            return <rect key={s.label} x={`${x}%`} y="0" width={`${(s.value / total) * 100}%`} height="14"
              strokeWidth={2} style={{ fill: color(s.tone), stroke: "rgb(var(--surface))" }}><title>{`${s.label}: ${s.value}`}</title></rect>;
          })}
        </g>
      </svg>
      <ul className="flex flex-wrap gap-x-4 gap-y-1 text-xs">
        {segments.map((s) => (
          <li key={s.label} className="inline-flex items-center gap-1.5">
            <span aria-hidden className="size-2.5 rounded-sm" style={{ background: color(s.tone) }} />
            <span className="text-fg-muted">{s.label}</span>
            <span className="tabular font-semibold">{s.value}</span>
            <span className="text-fg-subtle tabular">({total ? Math.round((s.value / total) * 100) : 0}%)</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

// ---------------------------------------------------------------- Heat grid
export function HeatGrid({ rows, cols, values, label, tone = "danger" }: {
  rows: string[]; cols: string[]; values: number[][]; label: string; tone?: Tone;
}) {
  const [ref, w] = useWidth<HTMLDivElement>();
  const ex = useExplorer(rows.length * cols.length, cols.length);
  const labelW = Math.min(130, Math.round(w * 0.32));
  const cw = Math.max(4, (w - labelW) / Math.max(1, cols.length));
  const CH = 22, GAP = 2, HEAD = 18;
  const max = Math.max(1, ...values.flat());
  const H = HEAD + rows.length * (CH + GAP);
  const ar = ex.i != null ? Math.floor(ex.i / cols.length) : -1;
  const ac = ex.i != null ? ex.i % cols.length : -1;
  const every = Math.max(1, Math.ceil(cols.length / Math.max(1, Math.floor((w - labelW) / 44))));
  const pick = (e: PointerEvent<HTMLDivElement>) => {
    const r = e.currentTarget.getBoundingClientRect();
    const c = Math.floor((e.clientX - r.left - labelW) / cw);
    const ro = Math.floor((e.clientY - r.top - HEAD) / (CH + GAP));
    ex.setI(c >= 0 && c < cols.length && ro >= 0 && ro < rows.length ? ro * cols.length + c : null);
  };
  return (
    <div ref={ref} className="relative w-full">
      <svg role="img" aria-label={label} width={w} height={H} className="block max-w-full">
        {cols.map((c, ci) => ci % every === 0 && (
          <text key={c + ci} x={labelW + ci * cw + cw / 2} y={HEAD / 2} textAnchor="middle" dominantBaseline="central" className="text-2xs"
            style={{ fill: "rgb(var(--text-subtle))" }}>{clip(c, Math.max(3, Math.floor((cw * every) / 6)))}</text>
        ))}
        {rows.map((r, ri) => (
          <g key={r}>
            <text x={0} y={HEAD + ri * (CH + GAP) + CH / 2} dominantBaseline="central" className="text-xs" style={{ fill: "rgb(var(--text-muted))" }}>
              {clip(r, Math.floor(labelW / 6.6))}
            </text>
            {cols.map((_c, ci) => {
              const v = values[ri]?.[ci] ?? 0;
              const on = ri === ar && ci === ac;
              return (
                <rect key={ci} x={labelW + ci * cw + GAP / 2} y={HEAD + ri * (CH + GAP)} width={cw - GAP} height={CH} rx={3}
                  strokeWidth={on ? 2 : 0}
                  style={{ fill: v ? color(tone, 0.18 + 0.82 * (v / max)) : "rgb(var(--surface-2))", stroke: "rgb(var(--text))" }} />
              );
            })}
          </g>
        ))}
      </svg>
      <Explorer label={`${label} — explore cells`} n={rows.length * cols.length} i={ex.i}
        valueText={ar >= 0 ? `${rows[ar]}, ${cols[ac]}: ${values[ar]?.[ac] ?? 0}` : "Use arrow keys to read each cell"}
        onKeyDown={ex.onKeyDown} onFocus={ex.onFocus} onBlur={ex.onBlur} onPointerMove={pick} onPointerLeave={() => ex.setI(null)} />
      {ar >= 0 && (
        <Tooltip x={labelW + ac * cw + cw / 2} y={HEAD + ar * (CH + GAP)} width={w}>
          <p className="font-semibold break-all">{rows[ar]}</p>
          <p className="text-fg-muted">{cols[ac]} · <span className="tabular font-semibold text-fg">{values[ar]?.[ac] ?? 0}</span></p>
        </Tooltip>
      )}
      <div className="mt-2 flex items-center justify-end gap-1.5 text-2xs text-fg-subtle" aria-hidden>
        <span>0</span>
        {[0.18, 0.45, 0.72, 1].map((a) => <span key={a} className="h-2.5 w-5 rounded-sm" style={{ background: color(tone, a) }} />)}
        <span className="tabular">{max}</span>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- Status dot
export function StatusDot({ tone, label }: { tone: Tone; label: string }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span aria-hidden className="relative inline-flex size-2.5">
        <span className="absolute inset-0 rounded-full opacity-30 scale-150" style={{ background: color(tone) }} />
        <span className="relative size-2.5 rounded-full" style={{ background: color(tone) }} />
      </span>
      <span className="sr-only">{label}</span>
    </span>
  );
}
