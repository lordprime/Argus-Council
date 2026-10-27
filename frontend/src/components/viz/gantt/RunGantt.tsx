// Run Gantt: plan steps, per-agent activity (thinking / tool calls / waiting) and sub-councils on a
// shared time axis. Left: a sticky table (name, status, start, end, duration, cost). Right: bars.
// Horizontal scroll lives inside the chart region only. Columns collapse by the component's own width
// (not the viewport): the table is capped at 45% so the bars always keep >= 55% of the pane.

import { ChevronDown, ChevronRight, CircleDashed, Maximize2, ZoomIn, ZoomOut } from "lucide-react";
import {
  Fragment,
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent,
  type ReactNode,
} from "react";

import { EmptyState, IconButton, STATUS, StatusPill } from "@/components/ui";
import { RoleAvatar, roleStyle } from "@/components/ui/role";
import { cn } from "@/lib/cn";
import { usd } from "@/lib/format";
import { isTerminal, type RunState } from "@/stores/run";

import { buildGantt, fmtDur, fmtOffset, tickStep, type Row, type Seg } from "./model";

export interface RunGanttProps {
  run: RunState;
  onSelect?: (ref: { itemId?: string; nodeId?: string }) => void;
}

type View = "all" | "steps" | "agents";
type Sort = "roster" | "start";

const ROW_H = 36;
const MIN_SPAN = 10_000;
const ZOOM_MIN = 0.5;
const ZOOM_MAX = 64;
const HATCH = "repeating-linear-gradient(135deg, currentColor 0 1.5px, transparent 1.5px 6px)";
const STRIPE = "repeating-linear-gradient(90deg, currentColor 0 2px, transparent 2px 7px)";

type ColKey = "status" | "start" | "end" | "dur" | "cost";
const COL_W: Record<ColKey, number> = { status: 112, start: 64, end: 64, dur: 72, cost: 64 };
const COL_LABEL: Record<ColKey, string> = { status: "Status", start: "Start", end: "End", dur: "Duration", cost: "Cost" };
/** Container width below which only the name (+ status icon) column remains. */
export const GANTT_NARROW = 900;
/** Container width below which start / end are hidden. */
export const GANTT_MID = 1200;
/** The table never takes more than this share of the component width. */
const TABLE_MAX = 0.45;
const NAME_MIN = 96;
const NAME_MAX = 220;
const TABLE_BORDER = 4; // 3px role accent + 1px divider

/** Pure column layout for a given component width (exported for tests). */
export function ganttColumns(containerW: number): { cols: ColKey[]; nameW: number; tableW: number } {
  const cols: ColKey[] = containerW < GANTT_NARROW ? []
    : containerW < GANTT_MID ? ["status", "dur", "cost"]
    : ["status", "start", "end", "dur", "cost"];
  const other = cols.reduce((n, c) => n + COL_W[c], 0);
  const nameW = Math.round(Math.min(NAME_MAX, Math.max(NAME_MIN, containerW * TABLE_MAX - TABLE_BORDER - other)));
  return { cols, nameW, tableW: nameW + other + TABLE_BORDER };
}

const TONE_TEXT: Record<string, string> = {
  neutral: "text-neutral", info: "text-info", success: "text-success",
  warning: "text-warning", danger: "text-danger", accent: "text-accent",
};

function StatusIcon({ status }: { status: string }) {
  const s = STATUS[status];
  const Icon = s?.icon ?? CircleDashed;
  return (
    <span className={cn("inline-flex shrink-0", TONE_TEXT[s?.tone ?? "neutral"])} title={s?.label ?? status}>
      <Icon className={cn("size-3.5", s?.spin && "motion-safe:animate-spin")} aria-hidden />
      <span className="sr-only">{s?.label ?? status}</span>
    </span>
  );
}

/** Observed clientWidth of an element (0 until measured, e.g. in jsdom). */
function useWidth<T extends HTMLElement>(): [(el: T | null) => void, number] {
  const [el, setEl] = useState<T | null>(null);
  const [w, setW] = useState(0);
  useEffect(() => {
    if (!el) return;
    const measure = () => setW(el.clientWidth);
    measure();
    if (typeof ResizeObserver === "undefined") {
      window.addEventListener("resize", measure);
      return () => window.removeEventListener("resize", measure);
    }
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [el]);
  return [setEl, w];
}

function useNow(live: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!live) return;
    setNow(Date.now());
    const h = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(h);
  }, [live]);
  return now;
}

interface Tip { seg: Seg; left: number; top: number; above: boolean }

export default function RunGantt({ run, onSelect }: RunGanttProps) {
  const live = !isTerminal(run.status);
  const now = useNow(live);
  const model = useMemo(() => buildGantt(run, now), [run, now]);
  const uid = useId().replace(/[^a-zA-Z0-9_-]/g, "");
  const tipId = `gantt-tip-${uid}`;

  const [zoom, setZoom] = useState(1);
  const [view, setView] = useState<View>("all");
  const [sort, setSort] = useState<Sort>("roster");
  const [collapsed, setCollapsed] = useState<ReadonlySet<string>>(() => new Set());
  const [tip, setTip] = useState<Tip | null>(null);

  const [boxRef, boxW] = useWidth<HTMLElement>();
  const [scrollEl, scrollW] = useWidth<HTMLDivElement>();
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const setScrollRef = useCallback((el: HTMLDivElement | null) => { scrollRef.current = el; scrollEl(el); }, [scrollEl]);
  // jsdom / first paint: fall back to the window width when the box has not been measured.
  const containerW = boxW > 0 ? boxW : (typeof window !== "undefined" && window.innerWidth) || 1024;
  const { cols, nameW, tableW } = ganttColumns(containerW);
  const narrow = cols.length === 0;
  const viewW = Math.max((scrollW > 0 ? scrollW : containerW - 2) - tableW, 120);

  const spanMs = Math.max(model.tEnd - model.t0, MIN_SPAN);
  const pxPerMs = (Math.max(viewW - 24, 120) / spanMs) * zoom;
  const chartW = Math.max(viewW, Math.ceil(spanMs * pxPerMs) + 24);
  const x = useCallback((t: number) => (t - model.t0) * pxPerMs, [model.t0, pxPerMs]);
  const step = tickStep(pxPerMs);
  const nowX = live ? x(Math.max(now, model.t0)) : null;

  const agents = useMemo(() => {
    if (sort === "roster") return model.agents;
    return [...model.agents].sort((a, b) => (a.start ?? Infinity) - (b.start ?? Infinity));
  }, [model.agents, sort]);

  const select = (seg: Seg) => {
    setTip(null);
    onSelect?.(seg.itemId ? { itemId: seg.itemId, nodeId: seg.nodeId } : { nodeId: seg.nodeId });
  };
  const showTip = (seg: Seg, el: HTMLElement) => {
    const r = el.getBoundingClientRect();
    const vw = window.innerWidth || 1024;
    const vh = window.innerHeight || 768;
    const above = r.bottom + 150 > vh && r.top > 160;
    setTip({ seg, left: Math.max(8, Math.min(r.left, vw - 280)), top: above ? r.top - 6 : r.bottom + 6, above });
  };

  const onBarKey = (e: KeyboardEvent<HTMLButtonElement>) => {
    if (e.key === "Escape") { setTip(null); return; }
    if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End"].includes(e.key)) return;
    const root = scrollRef.current;
    if (!root) return;
    const all = Array.from(root.querySelectorAll<HTMLButtonElement>("[data-bar]"));
    const cur = e.currentTarget;
    const i = all.indexOf(cur);
    const row = Number(cur.dataset.row);
    const inRow = (r: number) => all.filter((b) => Number(b.dataset.row) === r);
    let target: HTMLButtonElement | undefined;
    if (e.key === "ArrowRight") target = all[i + 1];
    else if (e.key === "ArrowLeft") target = all[i - 1];
    else if (e.key === "Home") target = inRow(row)[0];
    else if (e.key === "End") target = inRow(row).at(-1);
    else {
      const rows = [...new Set(all.map((b) => Number(b.dataset.row)))];
      const ri = rows.indexOf(row) + (e.key === "ArrowDown" ? 1 : -1);
      const cand = ri >= 0 && ri < rows.length ? inRow(rows[ri]!) : [];
      const cx = cur.offsetLeft;
      target = cand.reduce<HTMLButtonElement | undefined>((best, b) =>
        !best || Math.abs(b.offsetLeft - cx) < Math.abs(best.offsetLeft - cx) ? b : best, undefined);
    }
    if (target) {
      e.preventDefault();
      target.focus();
      target.scrollIntoView({ block: "nearest", inline: "nearest" });
    }
  };

  const toggleGroup = (id: string) => setCollapsed((s) => {
    const n = new Set(s);
    if (n.has(id)) n.delete(id); else n.add(id);
    return n;
  });

  // Flatten visible rows so every bar knows its row index (keyboard order = row order).
  let rowIndex = 0;
  const showSteps = view !== "agents" && model.steps.length > 0;
  const showAgents = view !== "steps" && agents.length > 0;
  const showSubs = view !== "steps" && model.groups.length > 0;
  const empty = !model.steps.length && !model.agents.some((a) => a.segs.length) && !model.groups.length;

  const gridStyle: CSSProperties = {
    width: chartW,
    backgroundImage: "linear-gradient(to right, rgb(var(--border) / 0.7) 1px, transparent 1px)",
    backgroundSize: `${step * pxPerMs}px 100%`,
  };

  const ticks: number[] = [];
  for (let t = 0; t <= spanMs + step && ticks.length < 400; t += step) ticks.push(t);

  const renderRow = (row: Row, opts: { indent?: boolean; toggle?: ReactNode; extra?: ReactNode } = {}) => {
    const idx = rowIndex++;
    const rs = roleStyle(row.role);
    const open = row.segs.some((s) => s.open);
    const dur = row.start != null && row.end != null ? row.end - row.start : undefined;
    return (
      <div key={row.id} role="row" className="flex border-b border-border/60 hover:bg-surface-2/50" style={{ height: ROW_H }}>
        <div className={cn("sticky left-0 z-sticky flex shrink-0 items-stretch bg-surface border-r border-border border-l-[3px]",
          row.kind === "group" ? "border-l-warning" : rs.border)} style={{ width: tableW }}>
          <div role="rowheader" className={cn("flex min-w-0 shrink-0 items-center gap-2 px-2 text-sm", opts.indent && "pl-6")} style={{ width: nameW }}>
            {opts.toggle}
            {row.kind === "group"
              ? null
              : <RoleAvatar role={row.role} overseer={row.overseer} size={20}
                  label={row.kind === "step" ? `assignee role ${row.role ?? "none"}` : `${row.role ?? "agent"}${row.overseer ? " overseer" : ""}`} />}
            <span className="min-w-0 flex-1 truncate" title={row.label}>{row.label}</span>
            {narrow && row.status && <StatusIcon status={row.status} />}
          </div>
          {cols.map((c) => (
            <div key={c} role="cell" className={cn("flex min-w-0 shrink-0 items-center px-2 text-xs tabular-nums", c !== "dur" && "text-fg-muted")}
              style={{ width: COL_W[c] }}>
              {c === "status" ? (row.kind === "agent" || row.kind === "submember"
                ? <span className="truncate">{row.status}</span>
                : <StatusPill status={row.status} className="max-w-full truncate" />)
                : c === "start" ? (row.start != null ? fmtOffset(row.start - model.t0) : "—")
                : c === "end" ? (row.end == null ? "—" : open && live ? "now" : fmtOffset(row.end - model.t0))
                : c === "dur" ? fmtDur(dur)
                : row.costUsd ? usd(row.costUsd) : "—"}
            </div>
          ))}
        </div>
        <div role="cell" className="relative shrink-0" style={gridStyle}>
          {opts.extra}
          {row.segs.length === 0 && <span className="sr-only">No activity</span>}
          {row.segs.map((seg) => (
            <Bar key={seg.id} seg={seg} row={idx} left={x(seg.start)} width={Math.max(4, (seg.end - seg.start) * pxPerMs)}
              t0={model.t0} live={live} describedBy={tip?.seg.id === seg.id ? tipId : undefined}
              onActivate={select} onShow={showTip} onHide={() => setTip(null)} onKey={onBarKey} />
          ))}
          {nowX != null && (
            <div aria-hidden className="pointer-events-none absolute inset-y-0 w-0.5 -translate-x-1/2 bg-danger/80" style={{ left: nowX }} />
          )}
        </div>
      </div>
    );
  };

  // Dependency arrows between step bars (drawn once over the whole step section).
  const stepArrows = () => {
    const pos = new Map(model.steps.map((s, i) => [s.id, { i, seg: s.segs[0] }]));
    const paths: string[] = [];
    for (const [i, s] of model.steps.entries()) {
      const to = s.segs[0];
      if (!to) continue;
      for (const d of s.deps) {
        const from = pos.get(d);
        if (!from?.seg) continue;
        const x1 = x(from.seg.end);
        const y1 = from.i * ROW_H + ROW_H / 2;
        const x2 = x(to.start);
        const y2 = i * ROW_H + ROW_H / 2;
        if (x2 - 4 >= x1 + 8) paths.push(`M${x1},${y1} H${x1 + 8} V${y2} H${x2 - 2}`);
        else {
          const ym = y1 + (y2 > y1 ? ROW_H / 2 : -ROW_H / 2);
          paths.push(`M${x1},${y1} h8 V${ym} H${x2 - 10} V${y2} H${x2 - 2}`);
        }
      }
    }
    if (!paths.length) return null;
    return (
      <svg aria-hidden className="pointer-events-none absolute left-0 top-0 overflow-visible text-fg-subtle"
        width={chartW} height={model.steps.length * ROW_H}>
        <defs>
          <marker id={`gantt-arrow-${uid}`} viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto">
            <path d="M0,0 L8,4 L0,8 z" fill="currentColor" />
          </marker>
        </defs>
        {paths.map((d, k) => (
          <path key={k} d={d} fill="none" stroke="currentColor" strokeWidth={1.25} markerEnd={`url(#gantt-arrow-${uid})`} />
        ))}
      </svg>
    );
  };

  const section = (key: string, title: string, accent: string, children: ReactNode) => (
    <div role="rowgroup" aria-label={title} key={key}>
      <div role="row" className="flex border-b border-border bg-surface-2" style={{ height: 28 }}>
        <div role="cell" className={cn("sticky left-0 z-sticky flex items-center gap-2 bg-surface-2 border-l-[3px] px-2 text-2xs uppercase tracking-wide text-fg-muted", accent)}>
          {title}
        </div>
      </div>
      {children}
    </div>
  );

  return (
    <section ref={boxRef} aria-label="Run Gantt chart" data-cols={narrow ? "narrow" : cols.length < 5 ? "mid" : "wide"}
      className="flex w-full min-w-0 flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2">
        <div role="group" aria-label="Rows to show" className="inline-flex gap-1 rounded-lg bg-surface-2 p-1">
          {(["all", "steps", "agents"] as const).map((v) => (
            <button key={v} type="button" aria-pressed={view === v} onClick={() => setView(v)}
              className={cn("h-7 rounded-md px-2.5 text-sm capitalize transition-colors duration-fast",
                view === v ? "bg-surface text-fg shadow-1 font-medium" : "text-fg-muted hover:text-fg")}>
              {v}
            </button>
          ))}
        </div>
        <label className="flex items-center gap-1.5 text-sm text-fg-muted">
          <span>Sort</span>
          <select value={sort} onChange={(e) => setSort(e.target.value as Sort)}
            className="h-8 rounded-md border border-border-strong bg-surface px-2 text-sm text-fg">
            <option value="roster">Roster order</option>
            <option value="start">First activity</option>
          </select>
        </label>
        <div className="ml-auto flex items-center gap-1">
          <IconButton label="Zoom out" icon={ZoomOut} onClick={() => setZoom((z) => Math.max(ZOOM_MIN, z / 1.5))} disabled={zoom <= ZOOM_MIN} />
          <span className="w-12 text-center text-xs tabular-nums text-fg-muted" aria-live="polite">{Math.round(zoom * 100)}%</span>
          <IconButton label="Zoom in" icon={ZoomIn} onClick={() => setZoom((z) => Math.min(ZOOM_MAX, z * 1.5))} disabled={zoom >= ZOOM_MAX} />
          <IconButton label="Fit to width" icon={Maximize2} onClick={() => setZoom(1)} />
        </div>
      </div>

      {empty ? (
        <EmptyState title="No activity yet" body="Bars appear here as members think, call tools and complete plan steps." />
      ) : (
        <div ref={setScrollRef} role="region" aria-label="Timeline, scroll horizontally" tabIndex={0}
          className="relative max-h-[70vh] w-full min-w-0 overflow-auto rounded-lg border border-border bg-surface">
          <div role="table" aria-label="Run activity timeline" aria-describedby={`gantt-legend-${uid}`} style={{ width: "max-content", minWidth: "100%" }}>
            <div role="rowgroup" className="sticky top-0 z-rail">
              <div role="row" className="flex border-b border-border bg-surface" style={{ height: 32 }}>
                <div className="sticky left-0 z-rail flex shrink-0 bg-surface border-r border-border border-l-[3px] border-l-transparent text-2xs uppercase tracking-wide text-fg-subtle"
                  style={{ width: tableW }}>
                  <div role="columnheader" className="flex shrink-0 items-center px-2" style={{ width: nameW }}>Name</div>
                  {cols.map((c) => (
                    <div key={c} role="columnheader" className="flex shrink-0 items-center px-2" style={{ width: COL_W[c] }}>{COL_LABEL[c]}</div>
                  ))}
                </div>
                <div role="columnheader" data-testid="gantt-timeline" className="relative shrink-0 bg-surface" style={{ width: chartW }}>
                  <span className="sr-only">Timeline</span>
                  {ticks.map((t) => (
                    <span key={t} aria-hidden className="absolute top-0 flex h-full items-end pb-1 pl-1 text-2xs tabular-nums text-fg-subtle border-l border-border"
                      style={{ left: t * pxPerMs }}>
                      {fmtOffset(t)}
                    </span>
                  ))}
                  {nowX != null && (
                    <span data-testid="gantt-now" className="absolute top-1 z-sticky -translate-x-1/2 whitespace-nowrap rounded-full bg-danger px-2 text-2xs leading-5 text-danger-fg shadow-1"
                      style={{ left: nowX }}>
                      Now <span className="tabular-nums">{fmtOffset(now - model.t0)}</span>
                    </span>
                  )}
                </div>
              </div>
            </div>

            {showSteps && section("steps", `Plan · ${model.steps.length} steps`, "border-l-accent",
              model.steps.map((r, i) => renderRow(r, { extra: i === 0 ? stepArrows() : undefined })))}
            {showAgents && section("agents", `Agents · ${agents.length}`, "border-l-info", agents.map((r) => renderRow(r)))}
            {showSubs && section("subs", `Sub-councils · ${model.groups.length}`, "border-l-warning",
              model.groups.map((g) => {
                const isOpen = !collapsed.has(g.row.id);
                return (
                  <Fragment key={g.row.id}>
                    {renderRow(g.row, {
                      toggle: (
                        <button type="button" aria-expanded={isOpen} onClick={() => toggleGroup(g.row.id)}
                          aria-label={`${isOpen ? "Collapse" : "Expand"} sub-council ${g.row.label}`}
                          className="-ml-1 inline-flex size-6 shrink-0 items-center justify-center rounded-md text-fg-muted hover:bg-surface-2">
                          {isOpen ? <ChevronDown className="size-4" aria-hidden /> : <ChevronRight className="size-4" aria-hidden />}
                        </button>
                      ),
                    })}
                    {isOpen && g.members.map((m) => renderRow(m, { indent: true }))}
                  </Fragment>
                );
              }))}
          </div>
        </div>
      )}

      <Legend id={`gantt-legend-${uid}`} live={live} />

      {tip && (
        <div id={tipId} role="tooltip"
          className="pointer-events-none fixed z-popover w-[268px] rounded-lg border border-border bg-surface p-2.5 text-xs text-fg shadow-3"
          style={{ left: tip.left, top: tip.top, transform: tip.above ? "translateY(-100%)" : undefined }}>
          <TipBody seg={tip.seg} t0={model.t0} live={live} />
        </div>
      )}
    </section>
  );
}

function barLabel(seg: Seg, t0: number, live: boolean) {
  const until = seg.open && live ? "now" : fmtOffset(seg.end - t0);
  const parts = [seg.title, seg.who, `${fmtOffset(seg.start - t0)} to ${until}`, fmtDur(seg.end - seg.start)];
  if (seg.status) parts.push(seg.status);
  return parts.join(", ");
}

function Bar({ seg, row, left, width, t0, live, describedBy, onActivate, onShow, onHide, onKey }: {
  seg: Seg; row: number; left: number; width: number; t0: number; live: boolean; describedBy?: string;
  onActivate: (s: Seg) => void; onShow: (s: Seg, el: HTMLElement) => void; onHide: () => void;
  onKey: (e: KeyboardEvent<HTMLButtonElement>) => void;
}) {
  const rs = roleStyle(seg.role);
  const failed = seg.status === "error" || seg.status === "failed" || seg.status === "timeout" || seg.status === "blocked";
  const base = "absolute flex items-center overflow-hidden text-left transition-[left,width] duration-base ease-out";
  const look: Record<Seg["kind"], string> = {
    think: cn("top-2 h-5 rounded-md text-surface", rs.bar),
    tool: cn("top-2 h-5 rounded-md border-[1.5px] border-current bg-surface", rs.fg),
    wait: "top-2 h-5 rounded-md border border-dashed border-warning bg-warning-subtle text-warning",
    step: cn("top-1.5 h-6 rounded-full border border-current", rs.bg, rs.fg),
    span: "top-2 h-5 rounded-md border border-dashed border-border-strong bg-surface-3 text-fg-muted",
  };
  const pattern = seg.kind === "tool" ? STRIPE : seg.kind === "wait" || seg.kind === "span" ? HATCH : null;
  return (
    <button type="button" data-bar="" data-row={row} aria-label={barLabel(seg, t0, live)} aria-describedby={describedBy}
      className={cn(base, look[seg.kind], failed && "ring-2 ring-danger ring-offset-1 ring-offset-surface")}
      style={{ left, width }}
      onClick={() => onActivate(seg)}
      onMouseEnter={(e) => onShow(seg, e.currentTarget)} onMouseLeave={onHide}
      onFocus={(e) => onShow(seg, e.currentTarget)} onBlur={onHide}
      onKeyDown={onKey}>
      {pattern && <span aria-hidden className="absolute inset-0 opacity-25" style={{ backgroundImage: pattern }} />}
      {width >= 56 && (
        <span aria-hidden className="relative truncate px-1.5 text-2xs">
          {seg.kind === "think" ? "thinking" : seg.kind === "tool" ? seg.title.replace(/^Tool · /, "") : seg.kind === "wait" ? "waiting" : seg.title}
        </span>
      )}
      {seg.open && <span aria-hidden className="absolute inset-y-0 right-0 w-1 bg-current opacity-60 motion-safe:animate-pulse" />}
    </button>
  );
}

function TipBody({ seg, t0, live }: { seg: Seg; t0: number; live: boolean }) {
  const tokens = seg.tokensIn != null || seg.tokensOut != null
    ? `${seg.tokensIn ?? 0} in / ${seg.tokensOut ?? 0} out` : null;
  return (
    <>
      <p className="font-medium text-sm leading-5 line-clamp-2">{seg.title}</p>
      <dl className="mt-1 grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-fg-muted">
        <dt>Who</dt><dd className="truncate text-fg">{seg.who}</dd>
        <dt>When</dt><dd className="tabular-nums text-fg">{fmtOffset(seg.start - t0)} → {seg.open && live ? "now" : fmtOffset(seg.end - t0)}</dd>
        <dt>Duration</dt><dd className="tabular-nums text-fg">{fmtDur(seg.end - seg.start)}</dd>
        {seg.status && <><dt>Status</dt><dd className="text-fg">{seg.status}</dd></>}
        {tokens && <><dt>Tokens</dt><dd className="tabular-nums text-fg">{tokens}</dd></>}
        {seg.costUsd != null && seg.costUsd > 0 && <><dt>Cost</dt><dd className="tabular-nums text-fg">{usd(seg.costUsd)}</dd></>}
      </dl>
    </>
  );
}

function Legend({ id, live }: { id: string; live: boolean }) {
  const sw = "relative inline-block h-3 w-6 overflow-hidden rounded-sm";
  return (
    <ul id={id} aria-label="Legend" className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-fg-muted">
      <li className="flex items-center gap-1.5"><span aria-hidden className={cn(sw, "rounded-full border border-accent bg-accent-subtle")} />Plan step</li>
      <li className="flex items-center gap-1.5"><span aria-hidden className={cn(sw, "bg-fg-muted")} />Thinking</li>
      <li className="flex items-center gap-1.5">
        <span aria-hidden className={cn(sw, "border-[1.5px] border-fg-muted text-fg-muted")}>
          <span className="absolute inset-0 opacity-25" style={{ backgroundImage: STRIPE }} />
        </span>Tool call
      </li>
      <li className="flex items-center gap-1.5">
        <span aria-hidden className={cn(sw, "border border-dashed border-warning bg-warning-subtle text-warning")}>
          <span className="absolute inset-0 opacity-25" style={{ backgroundImage: HATCH }} />
        </span>Waiting / held
      </li>
      <li className="flex items-center gap-1.5"><span aria-hidden className={cn(sw, "border border-dashed border-border-strong bg-surface-3")} />Sub-council</li>
      <li className="flex items-center gap-1.5"><span aria-hidden className={cn(sw, "ring-2 ring-danger ring-inset")} />Error</li>
      {live && <li className="flex items-center gap-1.5"><span aria-hidden className="inline-block h-3 w-0.5 bg-danger" />Now</li>}
      <li className="text-fg-subtle">Arrow keys move between bars; Enter opens the event.</li>
    </ul>
  );
}
