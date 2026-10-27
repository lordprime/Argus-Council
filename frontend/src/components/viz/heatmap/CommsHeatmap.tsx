// Communication heatmap: hex grid with one row per agent / sub-agent / peer and one column per time bin.
// Intensity encodes the chosen metric; colour is never the only cue (the tooltip on a hovered or focused
// cell prints its value, cells with errors carry a hatch pattern, and every cell has a full text label).

import { useEffect, useId, useMemo, useRef, useState, type KeyboardEvent } from "react";

import { Badge, Segmented } from "@/components/ui";
import { roleStyle } from "@/components/ui/role";
import { cn } from "@/lib/cn";
import type { RunState } from "@/stores/run";

import { RunStateBadge, VizPane, VizToolbar } from "../VizToolbar";

import {
  buildHeat, fmtBin, fmtClock, formatMetric, legendRanges, level, LEVELS, metricValue, type HeatCell, type Metric,
} from "./model";

export interface VizSelectRef { itemId?: string; nodeId?: string }
export interface CommsHeatmapProps { run: RunState; onSelect?: (ref: VizSelectRef) => void }

const R = 11; // hex circumradius
const W = Math.sqrt(3) * R; // hex width (pointy-top)
const DY = 1.5 * R; // row pitch
const PAD = 4;
/** Time-axis band under the grid: 11px labels (the SVG text floor) plus breathing room. */
const AXIS = 22;
const AXIS_FONT = 11;
/** Widest axis label ("12:38:30" in the mono face) and the side padding that keeps it unclipped. */
const AXIS_LABEL_W = 8 * AXIS_FONT * 0.62;
const PADX = Math.max(PAD, Math.ceil(AXIS_LABEL_W / 2 - W / 2) + 2);

// Literal class strings (Tailwind JIT). Sequential single-hue scale on the accent token; level 0 is the
// neutral surface so "none" reads as absence rather than as the lightest data value.
const LEVEL_FILL = ["fill-surface-3", "fill-accent/15", "fill-accent/35", "fill-accent/55", "fill-accent/80", "fill-accent"];
const LEVEL_BG = ["bg-surface-3", "bg-accent/15", "bg-accent/35", "bg-accent/55", "bg-accent/80", "bg-accent"];

const METRICS: { id: Metric; label: string }[] = [
  { id: "messages", label: "Messages" },
  { id: "tokens", label: "Tokens" },
  { id: "cost", label: "Cost" },
  { id: "errors", label: "Errors" },
];

const UNIT: Record<Metric, [string, string]> = {
  messages: ["message", "messages"],
  tokens: ["token", "tokens"],
  cost: ["", ""],
  errors: ["error", "errors"],
};

function describe(v: number, m: Metric): string {
  if (m === "cost") return formatMetric(v, m);
  const [one, many] = UNIT[m];
  return `${formatMetric(v, m)} ${v === 1 ? one : many}`;
}

function hexPoints(cx: number, cy: number, r: number): string {
  const pts: string[] = [];
  for (let k = 0; k < 6; k++) {
    const a = (Math.PI / 180) * (60 * k - 90);
    pts.push(`${(cx + r * Math.cos(a)).toFixed(2)},${(cy + r * Math.sin(a)).toFixed(2)}`);
  }
  return pts.join(" ");
}

const center = (r: number, c: number) => ({ x: PADX + W / 2 + c * W + (r % 2 ? W / 2 : 0), y: PAD + R + r * DY });

interface Tip { r: number; c: number; x: number; y: number }

export default function CommsHeatmap({ run, onSelect }: CommsHeatmapProps) {
  const uid = useId().replace(/[^a-zA-Z0-9_-]/g, "");
  const [metric, setMetric] = useState<Metric>("messages");
  const model = useMemo(() => buildHeat(run), [run]);
  const [active, setActive] = useState({ r: 0, c: 0 });
  const [hover, setHover] = useState<Tip | null>(null);
  const [focused, setFocused] = useState<Tip | null>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const gridRef = useRef<SVGSVGElement>(null);
  const moved = useRef(false);

  const max = useMemo(() => {
    let m = 0;
    for (const row of model.cells) for (const cell of row) m = Math.max(m, metricValue(cell, metric));
    return m;
  }, [model, metric]);
  const ranges = useMemo(() => legendRanges(max, metric), [max, metric]);

  const nRows = model.rows.length;
  const ar = Math.min(active.r, Math.max(0, nRows - 1));
  const ac = Math.min(active.c, model.cols - 1);

  const width = PADX * 2 + model.cols * W + W / 2;
  const gridH = PAD * 2 + 2 * R + Math.max(0, nRows - 1) * DY;
  const height = gridH + AXIS;
  const tickEvery = Math.max(1, Math.round(model.cols / 6));

  const cellLabel = (r: number, c: number, cell: HeatCell) => {
    const row = model.rows[r]!;
    const t0 = model.start + c * model.binMs;
    const parts = [`${row.node.label}${row.node.role ? ` (${row.node.role})` : ""}`, `${fmtClock(t0)} to ${fmtClock(t0 + model.binMs)}`, describe(metricValue(cell, metric), metric)];
    if (metric !== "errors" && cell.errors) parts.push(`${cell.errors} error${cell.errors === 1 ? "" : "s"}`);
    return parts.join(", ");
  };

  const tipFor = (r: number, c: number, el: Element | null): Tip => {
    const wrap = wrapRef.current?.getBoundingClientRect();
    const box = el?.getBoundingClientRect();
    if (!wrap || !box) return { r, c, x: 0, y: 0 };
    // Keep the 224px tooltip inside the pane so it never adds a horizontal scrollbar.
    const half = 112 + 4;
    const cx = box.left - wrap.left + box.width / 2;
    const x = wrap.width > half * 2 ? Math.min(wrap.width - half, Math.max(half, cx)) : cx;
    return { r, c, x, y: box.top - wrap.top };
  };

  // Move DOM focus with the roving tabindex after keyboard navigation.
  useEffect(() => {
    if (!moved.current) return;
    moved.current = false;
    const el = gridRef.current?.querySelector<SVGGElement>(`[data-cell="${ar}-${ac}"]`);
    el?.focus();
  }, [ar, ac]);

  const onKeyDown = (e: KeyboardEvent<SVGGElement>, r: number, c: number) => {
    let nr = r;
    let nc = c;
    switch (e.key) {
      case "ArrowRight": nc = Math.min(model.cols - 1, c + 1); break;
      case "ArrowLeft": nc = Math.max(0, c - 1); break;
      case "ArrowDown": nr = Math.min(nRows - 1, r + 1); break;
      case "ArrowUp": nr = Math.max(0, r - 1); break;
      case "Home": nc = 0; if (e.ctrlKey) nr = 0; break;
      case "End": nc = model.cols - 1; if (e.ctrlKey) nr = nRows - 1; break;
      case "Enter":
      case " ":
        e.preventDefault();
        onSelect?.({ nodeId: model.rows[r]!.node.id });
        return;
      case "Escape": setFocused(null); return;
      default: return;
    }
    e.preventDefault();
    if (nr !== r || nc !== c) {
      moved.current = true;
      setActive({ r: nr, c: nc });
    }
  };

  const tip = hover ?? focused;
  const tipCell = tip ? model.cells[tip.r]?.[tip.c] : undefined;
  const tipRow = tip ? model.rows[tip.r] : undefined;
  const tipId = `${uid}-tip`;
  const hatchId = `${uid}-hatch`;

  const titleId = `${uid}-title`;
  return (
    <VizPane labelledBy={titleId}>
      <VizToolbar titleId={titleId} title="Communication heatmap"
        meta={<>
          <RunStateBadge status={run.status} testId="heatmap-status" />
          <Badge>{nRows} node{nRows === 1 ? "" : "s"} · bin {fmtBin(model.binMs)}</Badge>
        </>}>
        <Segmented label="Metric" hideLabel size="sm" value={metric} onChange={setMetric}
          options={METRICS.map((m) => ({ value: m.id, label: m.label }))} />
      </VizToolbar>

      <div className="relative min-w-0 rounded-lg border border-border bg-surface text-fg">
        <svg aria-hidden width="0" height="0" className="absolute">
          <defs>
            <pattern id={hatchId} patternUnits="userSpaceOnUse" width="4" height="4" patternTransform="rotate(45)">
              <line x1="0" y1="0" x2="0" y2="4" className="stroke-danger" strokeWidth="1.6" />
            </pattern>
          </defs>
        </svg>
        {nRows === 0 ? (
          <p className="px-4 py-6 text-sm text-fg-muted">No communication yet.</p>
        ) : (
          <div ref={wrapRef} className="relative">
            <div className="overflow-x-auto overscroll-x-contain pt-2 pb-1" onScroll={() => { setHover(null); setFocused(null); }}>
              <div className="flex w-max">
                <div aria-hidden className="sticky left-0 z-sticky w-36 shrink-0 border-r border-border bg-surface pl-3 pr-2" style={{ paddingTop: PAD + R - DY / 2 }}>
                  {model.rows.map((row, r) => {
                    const s = roleStyle(row.node.role);
                    return (
                      <div key={row.node.id} className={cn("flex items-center gap-1.5 text-xs", r === ar && "font-semibold")} style={{ height: DY, paddingLeft: row.depth * 10 }}>
                        <s.icon className={cn("h-3 w-3 shrink-0", s.fg)} />
                        <span className="truncate">{row.node.label}</span>
                      </div>
                    );
                  })}
                </div>
                <svg
                  ref={gridRef}
                  role="grid"
                  aria-label={`Messages per agent over time, metric ${metric}. Use arrow keys to move between cells.`}
                  aria-rowcount={nRows}
                  aria-colcount={model.cols}
                  width={width}
                  height={height}
                  viewBox={`0 0 ${width} ${height}`}
                  className="block shrink-0"
                  onMouseLeave={() => setHover(null)}
                >
                  {model.rows.map((row, r) => (
                    <g key={row.node.id} role="row" aria-rowindex={r + 1}>
                      {model.cells[r]!.map((cell, c) => {
                        const { x, y } = center(r, c);
                        const v = metricValue(cell, metric);
                        const lv = level(v, max);
                        const isActive = r === ar && c === ac;
                        const isFocused = focused?.r === r && focused?.c === c;
                        return (
                          <g
                            key={c}
                            role="gridcell"
                            aria-colindex={c + 1}
                            aria-label={cellLabel(r, c, cell)}
                            aria-describedby={isFocused || (hover?.r === r && hover?.c === c) ? tipId : undefined}
                            data-cell={`${r}-${c}`}
                            data-level={lv}
                            tabIndex={isActive ? 0 : -1}
                            className="cursor-pointer outline-none group"
                            onClick={(e) => { setActive({ r, c }); onSelect?.({ nodeId: row.node.id }); e.currentTarget.focus(); }}
                            onKeyDown={(e) => onKeyDown(e, r, c)}
                            onFocus={(e) => { setActive({ r, c }); setFocused(tipFor(r, c, e.currentTarget)); }}
                            onBlur={() => setFocused(null)}
                            onMouseEnter={(e) => setHover(tipFor(r, c, e.currentTarget))}
                          >
                            <polygon
                              points={hexPoints(x, y, R - 1)}
                              className={cn(LEVEL_FILL[lv], "stroke-surface transition-[fill] duration-base group-hover:stroke-fg-muted",
                                isFocused && "stroke-fg")}
                              strokeWidth={isFocused ? 2 : 1}
                            />
                            {cell.errors > 0 && <polygon points={hexPoints(x, y, R - 2.5)} fill={`url(#${hatchId})`} data-testid="error-hatch" />}
                          </g>
                        );
                      })}
                    </g>
                  ))}
                  <g aria-hidden>
                    {Array.from({ length: model.cols }, (_, c) => c).filter((c) => c % tickEvery === 0).map((c) => (
                      <text key={c} x={center(0, c).x} y={gridH + AXIS_FONT + 4} textAnchor="middle" fontSize={AXIS_FONT}
                        className="fill-fg-subtle font-mono" data-testid="heatmap-tick">
                        {fmtClock(model.start + c * model.binMs)}
                      </text>
                    ))}
                  </g>
                </svg>
              </div>
            </div>

            {tip && tipCell && tipRow && (
              <div
                id={tipId}
                role="tooltip"
                className="pointer-events-none absolute z-popover w-56 -translate-x-1/2 -translate-y-full rounded-lg border border-border bg-surface-2 p-2.5 text-xs shadow-3"
                style={{ left: tip.x, top: tip.y - 6 }}
              >
                <div className="flex items-center gap-1.5 font-semibold">
                  {(() => { const s = roleStyle(tipRow.node.role); return <s.icon aria-hidden className={cn("h-3.5 w-3.5", s.fg)} />; })()}
                  <span className="truncate">{tipRow.node.label}</span>
                  <span className="ml-auto font-mono text-fg-muted">{describe(metricValue(tipCell, metric), metric)}</span>
                </div>
                <div className="mt-0.5 font-mono text-fg-subtle">
                  {fmtClock(model.start + tip.c * model.binMs)}–{fmtClock(model.start + (tip.c + 1) * model.binMs)}
                </div>
                {Object.keys(tipCell.kinds).length > 0 ? (
                  <dl className="mt-1.5 grid grid-cols-[1fr_auto] gap-x-3 gap-y-0.5">
                    {Object.entries(tipCell.kinds).sort((a, b) => b[1] - a[1]).map(([k, n]) => (
                      <div key={k} className="contents"><dt className="text-fg-muted">{k.replace("_", " ")}</dt><dd className="text-right font-mono">{n}</dd></div>
                    ))}
                    {tipCell.tokens > 0 && <div className="contents"><dt className="text-fg-muted">tokens</dt><dd className="text-right font-mono">{formatMetric(tipCell.tokens, "tokens")}</dd></div>}
                    {tipCell.cost > 0 && <div className="contents"><dt className="text-fg-muted">cost</dt><dd className="text-right font-mono">{formatMetric(tipCell.cost, "cost")}</dd></div>}
                    {tipCell.errors > 0 && <div className="contents"><dt className="text-danger">errors</dt><dd className="text-right font-mono text-danger">{tipCell.errors}</dd></div>}
                  </dl>
                ) : (
                  <p className="mt-1 text-fg-muted">No activity</p>
                )}
              </div>
            )}
          </div>
        )}

        <footer className="flex flex-wrap items-center gap-x-3 gap-y-1.5 border-t border-border px-4 py-2 text-xs text-fg-muted">
          <span className="font-medium text-fg">{METRICS.find((m) => m.id === metric)!.label} per bin</span>
          <ul aria-label="Legend" className="flex flex-wrap items-center gap-1">
            {Array.from({ length: LEVELS }, (_, l) => (
              <li key={l} className="flex items-center gap-1 pr-1.5">
                <span aria-hidden className={cn("inline-block h-3 w-4 rounded-sm border border-border", LEVEL_BG[l])} />
                <span className="font-mono">{l === 0 ? "none" : ranges[l]}</span>
              </li>
            ))}
            <li className="flex items-center gap-1 pl-1">
              <svg aria-hidden width="16" height="12" className="rounded-sm border border-border">
                <rect width="16" height="12" fill={`url(#${hatchId})`} />
              </svg>
              <span>has errors</span>
            </li>
          </ul>
        </footer>
      </div>
    </VizPane>
  );
}
