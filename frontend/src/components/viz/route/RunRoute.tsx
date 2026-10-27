// "Route covered": the plan as a trail winding up a contour mountain from Start to the Deliverable,
// with pins for notable events, next to a chronological run log. Inline SVG, theme tokens only.

import {
  Fingerprint, Flag, MapPin, PenLine, ShieldCheck, Tent, TriangleAlert, Users, Vote, type LucideIcon,
} from "lucide-react";
import { useId, useLayoutEffect, useMemo, useRef, useState, type KeyboardEvent, type RefObject } from "react";

import { Badge } from "@/components/ui";
import { cn } from "@/lib/cn";
import type { RunState } from "@/stores/run";

import { fmtClock } from "../heatmap/model";
import { RunStateBadge, VizPane, VizToolbar } from "../VizToolbar";
import { buildRoute, type Milestone, type MilestoneKind, type Tone } from "./model";

export interface RunRouteProps { run: RunState; onSelect?: (ref: { itemId?: string; nodeId?: string }) => void }

const VW = 360;
const VH = 440;
/** Side margins in the viewBox so step labels beside pins near the edge are not clipped. */
const PADX = 56;
const VB_X = -PADX;
const VB_W = VW + PADX * 2;
/** Labels render at no less than this many CSS px, whatever the SVG's scale (the 11px text floor). */
export const LABEL_MIN_PX = 11;
/** Pins are hit-tested on a circle at least this radius in CSS px (a 24px target, WCAG 2.5.8). */
export const PIN_HIT_MIN_PX = 12;
/** Average glyph advance of the UI face as a share of the font size, for fitting labels to the space left. */
const GLYPH = 0.56;

/** CSS px per viewBox unit for the rendered SVG (1 until measured). */
function useSvgScale(ref: RefObject<SVGSVGElement | null>): number {
  const [scale, setScale] = useState(1);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const measure = () => {
      const r = el.getBoundingClientRect();
      // preserveAspectRatio "meet": the smaller ratio wins.
      const k = r.width > 0 && r.height > 0 ? Math.min(r.width / VB_W, r.height / VH) : 0;
      if (k > 0) setScale(k);
    };
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [ref]);
  return scale;
}

/** Truncate a label to the space between its pin and the viewBox edge. */
export function fitLabel(text: string, availUnits: number, fontUnits: number): string {
  const max = Math.max(4, Math.floor(availUnits / (fontUnits * GLYPH)));
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

const ICONS: Record<MilestoneKind, LucideIcon> = {
  start: MapPin, step: Tent, intercept: TriangleAlert, approval: ShieldCheck, vote: Vote,
  subcouncil: Users, protocol: Fingerprint, correction: PenLine, summit: Flag,
};

const KIND_LABEL: Record<MilestoneKind, string> = {
  start: "Start", step: "Camp", intercept: "Alert", approval: "Approval", vote: "Vote",
  subcouncil: "Sub-council", protocol: "Protocol call", correction: "Correction", summit: "Summit",
};

const PIN_FILL: Record<Tone, string> = {
  done: "fill-success", active: "fill-accent", pending: "fill-surface stroke-border-strong",
  alert: "fill-danger", warn: "fill-warning", info: "fill-info",
};
const PIN_ICON: Record<Tone, string> = {
  done: "text-surface", active: "text-accent-fg", pending: "text-fg-muted",
  alert: "text-danger-fg", warn: "text-surface", info: "text-surface",
};
const BADGE: Record<Tone, string> = {
  done: "bg-success text-surface", active: "bg-accent text-accent-fg", pending: "bg-surface border border-border-strong text-fg-muted",
  alert: "bg-danger text-danger-fg", warn: "bg-warning text-surface", info: "bg-info text-surface",
};
const TONE_TEXT: Record<Tone, string> = {
  done: "completed", active: "in progress", pending: "pending", alert: "needs attention", warn: "warning", info: "",
};

/** Trail centre-line, s ∈ [0, 1] from the trailhead (bottom) to the summit (top). */
function pt(s: number) {
  return { x: VW / 2 + 105 * Math.sin(s * Math.PI * 2.4) * (1 - 0.6 * s), y: VH - 30 - s * (VH - 70) };
}

function trailPath(from: number, to: number): string {
  if (to <= from) return "";
  const n = Math.max(2, Math.ceil((to - from) * 160));
  const parts: string[] = [];
  for (let i = 0; i <= n; i++) {
    const p = pt(from + ((to - from) * i) / n);
    parts.push(`${i ? "L" : "M"}${p.x.toFixed(1)},${p.y.toFixed(1)}`);
  }
  return parts.join(" ");
}

function contour(k: number): string {
  const cx = VW / 2 + 8 * k;
  const cy = VH - 120 - k * 62;
  const rx = 172 - k * 30;
  const ry = 112 - k * 18;
  const parts: string[] = [];
  for (let i = 0; i <= 48; i++) {
    const a = (i / 48) * Math.PI * 2;
    const w = 1 + 0.07 * Math.sin(3 * a + k) + 0.04 * Math.cos(5 * a - k);
    parts.push(`${i ? "L" : "M"}${(cx + rx * w * Math.cos(a)).toFixed(1)},${(cy + ry * w * Math.sin(a)).toFixed(1)}`);
  }
  return `${parts.join(" ")} Z`;
}

function pinLabel(m: Milestone): string {
  const bits = [`${KIND_LABEL[m.kind]}: ${m.title}`];
  if (TONE_TEXT[m.tone] && m.kind !== "start") bits.push(TONE_TEXT[m.tone]);
  if (m.who) bits.push(m.who);
  if (m.t > 0) bits.push(fmtClock(m.t));
  if (m.hash) bits.push(`ledger ${m.hash.slice(0, 12)}`);
  return bits.join(", ");
}

export default function RunRoute({ run, onSelect }: RunRouteProps) {
  const uid = useId().replace(/[^a-zA-Z0-9_-]/g, "");
  const model = useMemo(() => buildRoute(run), [run]);
  const [selected, setSelected] = useState<string | null>(null);
  const [hot, setHot] = useState<string | null>(null);
  const logRef = useRef<HTMLOListElement>(null);
  const svgRef = useRef<SVGSVGElement>(null);
  const scale = useSvgScale(svgRef);
  // Sizes in viewBox units that come out at least LABEL_MIN_PX / PIN_HIT_MIN_PX on screen.
  const labelUnits = Math.max(LABEL_MIN_PX, LABEL_MIN_PX / scale);
  const hitUnits = Math.max(PIN_HIT_MIN_PX, PIN_HIT_MIN_PX / scale);

  const total = model.steps + 1;
  const sCur = Math.min(1, model.current / total);
  const cur = pt(sCur);

  // Pin placement: trail milestones sit on the path; events fan out to alternating sides.
  const placed = useMemo(() => {
    const buckets = new Map<number, number>();
    return model.milestones.map((m) => {
      const s = Math.min(1, Math.max(0, m.anchor / total));
      const p = pt(s);
      if (m.kind === "start" || m.kind === "step" || m.kind === "summit") return { m, x: p.x, y: p.y, ax: p.x, ay: p.y };
      const key = Math.round(s * 24);
      const k = buckets.get(key) ?? 0;
      buckets.set(key, k + 1);
      const q = pt(Math.min(1, s + 0.005));
      const len = Math.hypot(q.x - p.x, q.y - p.y) || 1;
      const nx = -(q.y - p.y) / len;
      const ny = (q.x - p.x) / len;
      const side = k % 2 ? -1 : 1;
      const d = 24 + Math.floor(k / 2) * 18;
      const x = Math.min(VW + PADX - 12, Math.max(VB_X + 12, p.x + nx * d * side));
      const y = Math.min(VH - 12, Math.max(12, p.y + ny * d * side));
      return { m, x, y, ax: p.x, ay: p.y };
    });
  }, [model, total]);

  const select = (m: Milestone, fromMap: boolean) => {
    setSelected(m.id);
    onSelect?.({ itemId: m.itemId, nodeId: m.itemId ? undefined : m.nodeId });
    if (fromMap) {
      const li = Array.from(logRef.current?.querySelectorAll<HTMLElement>("[data-log]") ?? []).find((el) => el.dataset.log === m.id);
      li?.scrollIntoView({ block: "nearest" });
    }
  };
  const onPinKey = (e: KeyboardEvent<SVGGElement>, m: Milestone) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); select(m, true); }
  };

  const pct = model.steps ? Math.round((Math.min(model.done, model.steps) / model.steps) * 100) : model.reached ? 100 : 0;
  const events = model.milestones.filter((m) => m.kind !== "start" && m.kind !== "step" && m.kind !== "summit").length;

  const titleId = `${uid}-title`;
  return (
    <VizPane labelledBy={titleId}>
      <VizToolbar titleId={titleId} title="Route covered"
        meta={<>
          <RunStateBadge status={run.status} />
          <span data-testid="route-progress" className="inline-flex">
            <Badge>{model.done} of {model.steps} step{model.steps === 1 ? "" : "s"} · {pct}%</Badge>
          </span>
          <Badge>{events} event{events === 1 ? "" : "s"}</Badge>
        </>} />

      <div className="grid gap-4 md:grid-cols-[minmax(0,1fr)_minmax(0,22rem)]">
        <figure className="m-0 min-w-0 rounded-lg border border-border bg-surface-2">
          <svg
            ref={svgRef}
            role="group"
            aria-label={`Route map: ${model.steps} plan steps from start to deliverable, ${pct}% covered`}
            viewBox={`${VB_X} 0 ${VB_W} ${VH}`}
            className="block h-auto w-full max-h-[32rem]"
          >
            <g aria-hidden className="fill-none">
              {[0, 1, 2, 3, 4].map((k) => (
                <path key={k} d={contour(k)} className={cn("stroke-border-strong/50", k === 4 ? "fill-accent/[0.06]" : "fill-none")} strokeWidth={1} />
              ))}
            </g>
            <g aria-hidden className="fill-none">
              <path d={trailPath(sCur, 1)} className="stroke-fg-subtle" strokeWidth={2.5} strokeDasharray="6 6" strokeLinecap="round" data-testid="trail-remaining" />
              <path d={trailPath(0, sCur)} className="stroke-accent" strokeWidth={3.5} strokeLinecap="round" data-testid="trail-done" />
              {placed.filter((p) => p.x !== p.ax || p.y !== p.ay).map((p) => (
                <line key={p.m.id} x1={p.ax} y1={p.ay} x2={p.x} y2={p.y} className="stroke-border-strong" strokeWidth={1} strokeDasharray="2 2" />
              ))}
            </g>

            <g aria-hidden data-testid="current-marker">
              <circle cx={cur.x} cy={cur.y} r={9} className="fill-accent/40 origin-center [transform-box:fill-box] animate-ping motion-reduce:animate-none [[data-motion=reduced]_&]:animate-none" />
              <circle cx={cur.x} cy={cur.y} r={5} className="fill-accent stroke-surface" strokeWidth={2} />
            </g>

            {placed.map(({ m, x, y }) => {
              const Icon = ICONS[m.kind];
              const big = m.kind === "summit" || m.kind === "start";
              const r = big ? 12 : m.kind === "step" ? 10 : 8.5;
              const isSel = selected === m.id;
              const showText = m.kind === "step" || big;
              const right = x >= VW / 2;
              const avail = right ? VW + PADX - (x + r + 6) - 4 : x - r - 6 - VB_X - 4;
              return (
                <g
                  key={m.id}
                  role="button"
                  tabIndex={0}
                  aria-label={pinLabel(m)}
                  aria-pressed={isSel}
                  data-pin={m.id}
                  className="group cursor-pointer outline-none"
                  onClick={() => select(m, true)}
                  onKeyDown={(e) => onPinKey(e, m)}
                  onMouseEnter={() => setHot(m.id)}
                  onMouseLeave={() => setHot(null)}
                  onFocus={() => setHot(m.id)}
                  onBlur={() => setHot(null)}
                >
                  {/* Transparent hit circle: the pin is easy to point at even when drawn small. */}
                  <circle cx={x} cy={y} r={Math.max(r + 4, hitUnits)} fill="transparent" data-hit="" />
                  <circle cx={x} cy={y} r={r + 4} className={cn("fill-none stroke-focus-ring", isSel ? "opacity-100" : "opacity-0 group-hover:opacity-60 group-focus-visible:opacity-100")} strokeWidth={2} />
                  <circle cx={x} cy={y} r={r} className={cn(PIN_FILL[m.tone], m.tone !== "pending" && "stroke-surface")} strokeWidth={1.5} />
                  <Icon aria-hidden x={x - r * 0.55} y={y - r * 0.55} width={r * 1.1} height={r * 1.1} className={PIN_ICON[m.tone]} strokeWidth={2.25} />
                  {showText && (
                    <text
                      x={right ? x + r + 6 : x - r - 6}
                      y={y}
                      textAnchor={right ? "start" : "end"}
                      fontSize={labelUnits}
                      dominantBaseline="central"
                      data-testid="pin-label"
                      className={cn("font-medium", m.tone === "pending" ? "fill-fg-muted" : "fill-fg")}
                    >
                      {fitLabel(m.title, avail, labelUnits)}
                    </text>
                  )}
                </g>
              );
            })}
          </svg>
          <figcaption className="flex flex-wrap gap-x-3 gap-y-1 border-t border-border px-3 py-1.5 text-xs text-fg-muted">
            <span className="inline-flex items-center gap-1"><span aria-hidden className="inline-block h-0.5 w-4 bg-accent" /> covered</span>
            <span className="inline-flex items-center gap-1"><span aria-hidden className="inline-block w-4 border-t-2 border-dashed border-fg-subtle" /> remaining</span>
            <span className="inline-flex items-center gap-1"><TriangleAlert aria-hidden className="h-3 w-3 text-danger" /> alert</span>
            <span className="inline-flex items-center gap-1"><Flag aria-hidden className="h-3 w-3" /> deliverable</span>
          </figcaption>
        </figure>

        <div className="min-w-0">
          <h3 id={`${uid}-log`} className="mb-2 text-sm font-semibold">Run log</h3>
          <ol
            ref={logRef}
            aria-labelledby={`${uid}-log`}
            className="relative max-h-[32rem] space-y-2 overflow-y-auto pr-1 before:absolute before:bottom-3 before:left-3 before:top-3 before:w-px before:bg-border"
          >
            {model.milestones.map((m) => {
              const Icon = ICONS[m.kind];
              const isSel = selected === m.id;
              return (
                <li key={m.id} data-log={m.id} className="relative pl-8">
                  <span aria-hidden className={cn("absolute left-0 top-2 flex h-6 w-6 items-center justify-center rounded-full ring-2 ring-surface", BADGE[m.tone])}>
                    <Icon className="h-3.5 w-3.5" />
                  </span>
                  <button
                    type="button"
                    aria-current={isSel ? "true" : undefined}
                    onClick={() => select(m, false)}
                    className={cn(
                      "w-full rounded-lg border bg-surface p-2 text-left transition-colors duration-fast hover:bg-surface-2",
                      "focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-focus-ring",
                      isSel ? "border-accent" : hot === m.id ? "border-border-strong" : "border-border",
                    )}
                  >
                    <span className="flex items-baseline gap-2">
                      <span className="sr-only">{KIND_LABEL[m.kind]}: </span>
                      <span className="min-w-0 flex-1 truncate text-sm font-medium">{m.title}</span>
                      {m.t > 0 ? (
                        <time dateTime={new Date(m.t).toISOString()} className="shrink-0 font-mono text-2xs text-fg-subtle">{fmtClock(m.t)}</time>
                      ) : (
                        <span className="shrink-0 text-2xs text-fg-subtle">upcoming</span>
                      )}
                    </span>
                    {m.who && <span className="block text-xs text-fg-muted">{m.who}</span>}
                    {m.detail && <span className="mt-1 block rounded-md bg-surface-2 px-2 py-1 text-xs text-fg">{m.detail}</span>}
                    {m.hash && <code className="mt-1 block truncate font-mono text-2xs text-fg-muted">ledger {m.hash.slice(0, 16)}</code>}
                  </button>
                </li>
              );
            })}
          </ol>
        </div>
      </div>
    </VizPane>
  );
}
