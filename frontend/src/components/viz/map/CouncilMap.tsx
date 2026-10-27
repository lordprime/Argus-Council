// Isometric "energy flow" map of a council run: platforms for every participant, conduits for
// every exchange, light pulses for live traffic. Usable first: keyboard-focusable nodes, filters,
// pause/live, zoom/pan, a details panel and a throttled screen-reader summary.

import { Link2, Pause, Radio, X } from "lucide-react";
import { useCallback, useEffect, useId, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";

import { Badge, Button, IconButton } from "@/components/ui";
import { buildGraph } from "@/features/viz/model";
import type { CommEvent, SubCouncilInfo } from "@/features/viz/types";
import { cn } from "@/lib/cn";
import { isTerminal, type RunState } from "@/stores/run";

import { RunStateBadge, VizPane, VizToolbar, ZoomControls } from "../VizToolbar";

import { MAX_PULSES, usePulses, useReducedMotion, useThrottledAnnouncement } from "./hooks";
import { boxWithCards, computeLayout, iso, layoutKey, placeCards, pointAt, polyline, route, toPath, type Polyline } from "./layout";
import { MapNode, cardLines, cardSize, clip, glyphFor, money, statusLabel, type GraphNode, type Ledger } from "./MapNode";
import { EDGE_CLASS, EDGE_DASH, GROUPS, GROUP_OF, KIND_LABEL, SWATCH_CLASS, type GroupId } from "./style";

export interface CouncilMapProps {
  run: RunState;
  onSelect?: (ref: { itemId?: string; nodeId?: string }) => void;
}

interface View {
  k: number;
  x: number;
  y: number;
}

const MIN_K = 0.5;
const MAX_K = 4;
const ZOOM_STEP = 1.25;
const RECENT = 30;

const pairKey = (a: string, b: string) => (a < b ? `${a}\u0000${b}` : `${b}\u0000${a}`);

function commsOf(run: RunState): CommEvent[] {
  return (run as Partial<RunState> & { comms?: CommEvent[] }).comms ?? [];
}
function subsOf(run: RunState): Record<string, SubCouncilInfo> {
  const s = (run as Partial<RunState> & { subcouncils?: Record<string, SubCouncilInfo> | SubCouncilInfo[] }).subcouncils;
  if (!s) return {};
  return Array.isArray(s) ? Object.fromEntries(s.map((x) => [x.id, x])) : s;
}

function time(ts: string) {
  const d = new Date(ts);
  return Number.isNaN(d.getTime()) ? ts : d.toLocaleTimeString([], { hour12: false });
}

export default function CouncilMap({ run, onSelect }: CouncilMapProps) {
  const reduced = useReducedMotion();
  const titleId = `cm-title-${useId().replace(/[^a-zA-Z0-9_-]/g, "")}`;
  // A finished run has no more traffic: there is nothing to follow or pause, so "Live" is not offered.
  const ended = isTerminal(run.status);
  const [following, setLive] = useState(true);
  const live = following && !ended;
  const [frozen, setFrozen] = useState<RunState | null>(null);
  const view = live || ended || !frozen ? run : frozen;
  const [hidden, setHidden] = useState<ReadonlySet<GroupId>>(() => new Set());
  const [selected, setSelected] = useState<string | null>(null);
  const [focused, setFocused] = useState<string | null>(null);
  const [hover, setHover] = useState<string | null>(null);
  const [zoom, setZoom] = useState<View>({ k: 1, x: 0, y: 0 });
  const svgRef = useRef<SVGSVGElement>(null);
  const pool = useRef<(SVGCircleElement | null)[]>([]);
  const lastInteract = useRef(0);

  const comms = commsOf(view);
  const subs = useMemo(() => subsOf(view), [view]);
  const graph = useMemo(() => buildGraph(view), [view]);

  // Layout depends only on the node set, not on traffic, so it stays put while messages flow.
  const lkey = layoutKey(graph.nodes);
  const nodesRef = useRef(graph.nodes);
  nodesRef.current = graph.nodes;
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const layout = useMemo(() => computeLayout(nodesRef.current), [lkey]);

  const routes = useMemo(() => {
    const cache = new Map<string, Polyline>();
    return (a: string, b: string): Polyline | undefined => {
      const k = pairKey(a, b);
      let pl = cache.get(k);
      if (!pl) {
        const pa = layout.nodes.get(a < b ? a : b);
        const pb = layout.nodes.get(a < b ? b : a);
        if (!pa || !pb || pa === pb) return undefined;
        pl = polyline(route(pa, pb));
        cache.set(k, pl);
      }
      return pl;
    };
  }, [layout]);
  const routeFor = useCallback(
    (from: string, to: string) => {
      const pl = routes(from, to);
      return pl ? { pl, reverse: from > to } : undefined;
    },
    [routes],
  );

  const isVisible = useCallback((ev: CommEvent) => !hidden.has(GROUP_OF[ev.kind] ?? "status"), [hidden]);
  const pulsing = live && !reduced;
  usePulses({ enabled: pulsing, comms, routeFor, visible: isVisible, pool });

  // Conduits: one per node pair, coloured by the most recent visible kind, thickness ~ volume.
  const conduits = useMemo(() => {
    const byPair = new Map<string, { a: string; b: string; kind: string; count: number; lastTs: string }>();
    for (const e of graph.edges) {
      if (hidden.has(GROUP_OF[e.kind] ?? "status")) continue;
      const k = pairKey(e.from, e.to);
      const cur = byPair.get(k);
      if (!cur) byPair.set(k, { a: e.from, b: e.to, kind: e.kind, count: e.count, lastTs: e.lastTs });
      else {
        cur.count += e.count;
        if (e.lastTs >= cur.lastTs) Object.assign(cur, { kind: e.kind, lastTs: e.lastTs });
      }
    }
    const out: { key: string; d: string; kind: string; count: number; pl: Polyline }[] = [];
    for (const [key, c] of byPair) {
      const pl = routes(c.a, c.b);
      if (pl) out.push({ key, d: toPath(pl.pts), kind: c.kind, count: c.count, pl });
    }
    return out;
  }, [graph.edges, hidden, routes]);

  // Per-node protocol summary (last ledger entry) and latest visible exchange.
  const ledgers = useMemo(() => {
    const m = new Map<string, Ledger>();
    for (const c of comms) {
      if (!c.ledger) continue;
      // Ledger details belong to the protocol endpoint (remote agent / MCP server), not the caller.
      for (const ref of [c.from, c.to]) if (ref.kind === "remote" || ref.kind === "mcp") m.set(ref.id, c.ledger);
    }
    return m;
  }, [comms]);
  const latest = useMemo(() => {
    for (let i = comms.length - 1; i >= 0; i--) if (isVisible(comms[i]!)) return comms[i];
    return undefined;
  }, [comms, isVisible]);
  const latestPair = latest ? pairKey(latest.from.id, latest.to.id) : null;

  const announce = useThrottledAnnouncement(
    latest && live ? `${latest.from.label} to ${latest.to.label}: ${latest.kind.replace("_", " ")}${latest.label ? `, ${clip(latest.label, 60)}` : ""}` : "",
  );

  // Paused: count traffic that arrived since freezing.
  const pendingNew = !live && !ended && frozen ? Math.max(0, commsOf(run).length - commsOf(frozen).length) : 0;
  const toggleLive = () => {
    if (live) {
      setFrozen(run);
      setLive(false);
    } else {
      setFrozen(null);
      setLive(true);
    }
  };

  const counts = useMemo(() => {
    const c: Partial<Record<GroupId, number>> = {};
    for (const e of comms) {
      const g = GROUP_OF[e.kind] ?? "status";
      c[g] = (c[g] ?? 0) + 1;
    }
    return c;
  }, [comms]);

  const byMemberCost = view.budget?.by_member;
  const nodeById = useMemo(() => new Map(graph.nodes.map((n) => [n.id, n])), [graph.nodes]);
  const zones = useMemo(() => {
    const z = new Map<string, number>();
    const subIds = new Set(graph.nodes.filter((n) => n.kind === "subcouncil").map((n) => n.id));
    for (const n of graph.nodes) if (n.parent && subIds.has(n.parent) && n.kind !== "subcouncil") z.set(n.parent, (z.get(n.parent) ?? 0) + 1);
    return new Map([...z].map(([id, k]) => [id, 1.5 + Math.max(0, k - 4) * 0.24 + 0.85]));
  }, [graph.nodes]);

  const compact = graph.nodes.length > 14;
  // Stable back-to-front order: never reorder the DOM on focus (that would blur the node and break Tab).
  const ordered = useMemo(
    () =>
      [...graph.nodes]
        .filter((n) => layout.nodes.has(n.id))
        .sort((a, b) => layout.nodes.get(a.id)!.depth - layout.nodes.get(b.id)!.depth || layout.nodes.get(a.id)!.z - layout.nodes.get(b.id)!.z),
    [graph.nodes, layout],
  );
  const raisedId = hover ?? focused ?? selected;

  // Collision-avoiding card placement, most important first: selection, the latest exchange, the
  // chair, overseers, members, then peers (user, tools, MCP, remote agents, sub-councils).
  const latestFrom = latest?.from.id;
  const latestTo = latest?.to.id;
  const cards = useMemo(() => {
    const rank = (n: GraphNode) =>
      n.id === selected ? 0
      : n.id === latestFrom || n.id === latestTo ? 1
      : n.role === "chair" && (n.kind === "member" || n.kind === "system") ? 2
      : n.kind === "overseer" ? 3
      : n.kind === "member" ? 4
      : 5;
    return placeCards(layout, ordered.map((n) => {
      const full = !compact || n.id === selected || n.id === latestFrom || n.id === latestTo;
      const lines = full ? cardLines({ node: n, cost: byMemberCost?.[n.id]?.usd, ledger: ledgers.get(n.id), sub: subs[n.id] }).length : 0;
      return { id: n.id, ...cardSize(n.label, lines), priority: rank(n) };
    }));
  }, [layout, ordered, compact, selected, latestFrom, latestTo, byMemberCost, ledgers, subs]);

  const activate = useCallback(
    (id: string) => {
      setSelected(id);
      onSelect?.({ nodeId: id });
    },
    [onSelect],
  );
  const onFocusChange = useCallback((id: string | null) => setFocused(id), []);
  const onHover = useCallback((id: string | null) => setHover(id), []);

  // Escape closes the details panel and returns focus to the node.
  useEffect(() => {
    if (!selected) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      const id = selected;
      setSelected(null);
      svgRef.current?.querySelector<SVGGElement>(`[data-node="${CSS.escape(id)}"]`)?.focus();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [selected]);

  /* ---------------------------------------------------------------- zoom & pan */
  const [bx, by, bw, bh] = useMemo(() => boxWithCards(layout, cards.values()), [layout, cards]);
  const zoomAt = useCallback(
    (factor: number, cx = bx + bw / 2, cy = by + bh / 2) => {
      lastInteract.current = Date.now();
      setZoom((z) => {
        const k = Math.min(MAX_K, Math.max(MIN_K, z.k * factor));
        const f = k / z.k;
        return { k, x: cx - (cx - z.x) * f, y: cy - (cy - z.y) * f };
      });
    },
    [bx, by, bw, bh],
  );
  // k = 1 with no pan is "fit": the viewBox is the bounding box of every platform and card.
  const fit = () => {
    lastInteract.current = Date.now();
    setZoom({ k: 1, x: 0, y: 0 });
  };
  const fitted = Math.abs(zoom.k - 1) < 1e-3 && Math.abs(zoom.x) < 0.5 && Math.abs(zoom.y) < 0.5;

  const toBox = (clientX: number, clientY: number): [number, number] | undefined => {
    const svg = svgRef.current;
    const ctm = svg?.getScreenCTM?.();
    if (!svg || !ctm || typeof svg.createSVGPoint !== "function") return undefined;
    const pt = svg.createSVGPoint();
    pt.x = clientX;
    pt.y = clientY;
    const r = pt.matrixTransform(ctm.inverse());
    return [r.x, r.y];
  };

  useEffect(() => {
    const svg = svgRef.current;
    if (!svg) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const p = toBox(e.clientX, e.clientY);
      zoomAt(e.deltaY < 0 ? 1.15 : 1 / 1.15, p?.[0], p?.[1]);
    };
    svg.addEventListener("wheel", onWheel, { passive: false });
    return () => svg.removeEventListener("wheel", onWheel);
  });

  const drag = useRef<{ id: number; x: number; y: number; vx: number; vy: number; scale: number } | null>(null);
  const onPointerDown = (e: ReactPointerEvent<SVGSVGElement>) => {
    if ((e.target as Element).closest?.("[data-node]") || e.button !== 0) return;
    const rect = svgRef.current?.getBoundingClientRect();
    const scale = rect && rect.width && rect.height ? Math.max(bw / rect.width, bh / rect.height) : 1;
    drag.current = { id: e.pointerId, x: e.clientX, y: e.clientY, vx: zoom.x, vy: zoom.y, scale };
    svgRef.current?.setPointerCapture?.(e.pointerId);
  };
  const onPointerMove = (e: ReactPointerEvent<SVGSVGElement>) => {
    const d = drag.current;
    if (!d || d.id !== e.pointerId) return;
    lastInteract.current = Date.now();
    setZoom((z) => ({ ...z, x: d.vx + (e.clientX - d.x) * d.scale, y: d.vy + (e.clientY - d.y) * d.scale }));
  };
  const endDrag = () => {
    drag.current = null;
  };

  // Live mode follows traffic: when zoomed in, glide to the latest exchange's target unless the
  // user has been steering the view in the last few seconds.
  const followId = live ? latest?.to.id : undefined;
  useEffect(() => {
    if (!followId || Date.now() - lastInteract.current < 5000) return;
    const p = layout.nodes.get(followId);
    if (!p) return;
    setZoom((z) => (z.k <= 1.05 ? z : { ...z, x: bx + bw / 2 - p.sx * z.k, y: by + bh / 2 - p.sy * z.k }));
  }, [followId, layout, bx, by, bw, bh]);

  /* ---------------------------------------------------------------- floor */
  const floor = useMemo(() => {
    const E = layout.extent;
    const step = E > 16 ? 2 : 1;
    const lines: string[] = [];
    for (let i = -E; i <= E; i += step) {
      lines.push(toPath([iso(i, -E), iso(i, E)]));
      lines.push(toPath([iso(-E, i), iso(E, i)]));
    }
    const corners = [iso(-E, -E), iso(E, -E), iso(E, E), iso(-E, E)].map(([x, y]) => `${x},${y}`).join(" ");
    return { d: lines.join(" "), corners };
  }, [layout.extent]);

  const sel = selected ? nodeById.get(selected) : undefined;

  return (
    <VizPane labelledBy={titleId} className="relative">
      <VizToolbar titleId={titleId} title="Council map"
        meta={<>
          {ended && <RunStateBadge status={run.status} />}
          <Badge>{graph.nodes.length} participant{graph.nodes.length === 1 ? "" : "s"}</Badge>
          <Badge>{conduits.length} link{conduits.length === 1 ? "" : "s"}</Badge>
        </>}
        zoom={<ZoomControls value={zoom.k} min={MIN_K} max={MAX_K} onZoomOut={() => zoomAt(1 / ZOOM_STEP)} onZoomIn={() => zoomAt(ZOOM_STEP)}
          onFit={fit} fitted={fitted} />}>
        {!ended && (
          <Button
            size="sm"
            variant={live ? "primary" : "secondary"}
            icon={live ? Radio : Pause}
            aria-pressed={live}
            onClick={toggleLive}
            title={live ? "Pause: freeze the map" : "Resume live updates"}
          >
            {live ? "Live" : "Paused"}
            {pendingNew > 0 && <span className="rounded-full bg-accent-subtle px-1.5 text-2xs text-accent">+{pendingNew}</span>}
          </Button>
        )}
        <div role="group" aria-label="Filter exchanges by kind" className="flex min-w-0 flex-1 flex-wrap gap-1">
          {GROUPS.map((g) => {
            const on = !hidden.has(g.id);
            return (
              <button
                key={g.id}
                type="button"
                aria-pressed={on}
                onClick={() =>
                  setHidden((h) => {
                    const n = new Set(h);
                    if (n.has(g.id)) n.delete(g.id);
                    else n.add(g.id);
                    return n;
                  })
                }
                className={cn(
                  "inline-flex h-7 items-center gap-1.5 rounded-full border px-2 text-xs transition-colors duration-fast",
                  on ? "border-border-strong bg-surface text-fg" : "border-border bg-surface-2 text-fg-subtle line-through",
                )}
              >
                <svg width="14" height="6" aria-hidden focusable="false">
                  <line x1="0" y1="3" x2="14" y2="3" className={SWATCH_CLASS[g.id]} strokeWidth="2.5" strokeDasharray={EDGE_DASH[g.kinds[0]]} />
                </svg>
                {g.label}
                <span className="tabular-nums text-fg-subtle">{counts[g.id] ?? 0}</span>
              </button>
            );
          })}
        </div>
      </VizToolbar>

      {/* Map */}
      <div className="relative h-[clamp(320px,62vh,680px)] overflow-hidden rounded-xl border border-border bg-canvas">
        <svg
          ref={svgRef}
          role="group"
          aria-label={`Council map: ${graph.nodes.length} participants, ${conduits.length} links. Tab to a participant and press Enter for details.`}
          viewBox={`${bx} ${by} ${bw} ${bh}`}
          preserveAspectRatio="xMidYMid meet"
          className={cn("absolute inset-0 h-full w-full select-none", zoom.k > 1.01 ? "touch-none cursor-grab" : "touch-pan-y")}
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={endDrag}
          onPointerCancel={endDrag}
        >
          <defs>
            <filter id="cm-glow" x="-50%" y="-50%" width="200%" height="200%">
              <feGaussianBlur stdDeviation="5" />
            </filter>
          </defs>
          <g transform={`translate(${zoom.x.toFixed(2)} ${zoom.y.toFixed(2)}) scale(${zoom.k.toFixed(3)})`}>
            <g aria-hidden="true">
              <polygon points={floor.corners} className="fill-surface-2/50 stroke-border" strokeWidth={1} />
              <path d={floor.d} className="fill-none stroke-border" strokeWidth={0.75} opacity={0.7} />
            </g>

            {/* Conduits */}
            <g aria-hidden="true" fill="none" strokeLinecap="round" strokeLinejoin="round">
              {conduits.map((c) => {
                const w = 1.25 + Math.min(4.5, Math.log2(c.count + 1) * 1.1);
                const hot = c.key === latestPair;
                const cls = EDGE_CLASS[c.kind] ?? "stroke-neutral";
                return (
                  <g key={c.key} data-edge={c.kind}>
                    <path d={c.d} className={cls} strokeWidth={w + 6} opacity={hot ? 0.3 : 0.12} />
                    <path d={c.d} className={cls} strokeWidth={hot ? w + 1 : w} strokeDasharray={EDGE_DASH[c.kind]} opacity={hot ? 1 : 0.8} />
                  </g>
                );
              })}
              {conduits
                .filter((c) => c.kind === "protocol")
                .map((c) => {
                  const [x, y] = pointAt(c.pl, 0.5);
                  return (
                    <g key={`l-${c.key}`} transform={`translate(${x.toFixed(1)} ${y.toFixed(1)})`} data-ledger-badge="">
                      <rect x={-17} y={-8} width={34} height={16} rx={8} className="fill-info-subtle stroke-info" strokeWidth={1} />
                      <Link2 x={-13} y={-5} width={10} height={10} className="text-info" aria-hidden focusable="false" />
                      <text x={-1} y={3.5} fontSize={8.5} className="fill-info font-mono">LDG</text>
                    </g>
                  );
                })}
            </g>

            {/* Pulses: fixed pool positioned by requestAnimationFrame (absent under reduced motion). */}
            {pulsing && (
              <g aria-hidden="true">
                {Array.from({ length: MAX_PULSES }, (_, i) => (
                  <circle key={i} ref={(el) => { pool.current[i] = el; }} r={3.5} visibility="hidden" data-pulse="" />
                ))}
              </g>
            )}

            {/* Platforms + cards, back to front */}
            {ordered.map((n) => {
              const p = layout.nodes.get(n.id)!;
              return (
                <MapNode
                  key={n.id}
                  p={p}
                  node={n}
                  cost={byMemberCost?.[n.id]?.usd}
                  ledger={ledgers.get(n.id)}
                  sub={subs[n.id]}
                  zone={zones.get(n.id)}
                  card={cards.get(n.id)!}
                  compact={compact}
                  selected={n.id === selected}
                  focused={n.id === focused}
                  highlight={!!latest && (latest.to.id === n.id || latest.from.id === n.id)}
                  reduced={reduced}
                  onActivate={activate}
                  onFocusChange={onFocusChange}
                  onHover={onHover}
                />
              );
            })}
            {raisedId && nodeById.get(raisedId) && layout.nodes.get(raisedId) && (
              <MapNode
                overlay
                p={layout.nodes.get(raisedId)!}
                card={cards.get(raisedId)!}
                node={nodeById.get(raisedId)!}
                cost={byMemberCost?.[raisedId]?.usd}
                ledger={ledgers.get(raisedId)}
                sub={subs[raisedId]}
                compact={compact}
                selected={raisedId === selected}
                focused={raisedId === focused}
                highlight={!!latest && (latest.to.id === raisedId || latest.from.id === raisedId)}
                reduced={reduced}
                onActivate={activate}
                onFocusChange={onFocusChange}
                onHover={onHover}
              />
            )}
          </g>
        </svg>

        {!graph.nodes.length && (
          <p className="absolute inset-0 grid place-items-center text-sm text-fg-muted">No participants yet.</p>
        )}

        {sel && (
          <DetailsPanel
            node={sel}
            cost={byMemberCost?.[sel.id]?.usd}
            ledger={ledgers.get(sel.id)}
            sub={subs[sel.id]}
            comms={comms}
            onClose={() => setSelected(null)}
            onPick={(ev) => onSelect?.({ itemId: ev.itemId ?? ev.id, nodeId: sel.id })}
          />
        )}
      </div>

      <Legend />
      <p className="sr-only" aria-live="polite" aria-atomic="true">{announce}</p>
    </VizPane>
  );
}

/* -------------------------------------------------------------------- details panel */

function DetailsPanel({ node, cost, ledger, sub, comms, onClose, onPick }: {
  node: GraphNode;
  cost?: number;
  ledger?: Ledger;
  sub?: SubCouncilInfo;
  comms: CommEvent[];
  onClose: () => void;
  onPick: (ev: CommEvent) => void;
}) {
  const recent = useMemo(() => {
    const out: CommEvent[] = [];
    for (let i = comms.length - 1; i >= 0 && out.length < RECENT; i--) {
      const c = comms[i]!;
      if (c.from.id === node.id || c.to.id === node.id) out.push(c);
    }
    return out;
  }, [comms, node.id]);
  const glyph = glyphFor(node);
  const st = statusLabel(node.status);

  return (
    <aside
      aria-label={`${node.label} details`}
      className={cn(
        "z-drawer flex flex-col overflow-hidden border border-border bg-surface shadow-3",
        "fixed inset-x-0 bottom-0 max-h-[60vh] rounded-t-xl",
        "sm:absolute sm:inset-x-auto sm:bottom-2 sm:right-2 sm:top-2 sm:max-h-none sm:w-80 sm:rounded-xl",
      )}
    >
      <header className="flex items-center gap-2 border-b border-border px-3 py-2">
        <glyph.icon className={cn("size-4 shrink-0", glyph.fg)} aria-hidden />
        <div className="min-w-0 flex-1">
          <h3 className="truncate text-sm font-semibold text-fg">{node.label}</h3>
          <p className="truncate text-xs text-fg-muted">
            {KIND_LABEL[node.kind]}
            {node.role && node.kind !== "subcouncil" ? ` · ${node.role}` : ""}
          </p>
        </div>
        <IconButton label="Close details" icon={X} onClick={onClose} />
      </header>
      <div className="overflow-y-auto px-3 py-2">
        <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs">
          <dt className="text-fg-subtle">Status</dt>
          <dd className="text-fg">{st ?? "—"}{node.active ? " · active" : ""}</dd>
          <dt className="text-fg-subtle">Messages</dt>
          <dd className="tabular-nums text-fg">{node.msgs}</dd>
          {cost != null && (
            <>
              <dt className="text-fg-subtle">Cost</dt>
              <dd className="tabular-nums text-fg">{money(cost)}</dd>
            </>
          )}
          {ledger && (
            <>
              <dt className="text-fg-subtle">Protocol</dt>
              <dd className="text-fg">{ledger.protocol} · {ledger.method} · {Math.round(ledger.latencyMs)} ms</dd>
              <dt className="text-fg-subtle">Ledger</dt>
              <dd className="font-mono text-fg">#{ledger.id} {ledger.hash.slice(0, 8)}</dd>
            </>
          )}
          {sub && (
            <>
              <dt className="text-fg-subtle">Task</dt>
              <dd className="text-fg">{sub.task}</dd>
              <dt className="text-fg-subtle">Mode</dt>
              <dd className="text-fg">{sub.mode} · depth {sub.depth} · {sub.status}</dd>
              <dt className="text-fg-subtle">Members</dt>
              <dd className="text-fg">{sub.members.map((m) => `${m.name}${m.overseer ? " (overseer)" : ""}`).join(", ") || "—"}</dd>
            </>
          )}
        </dl>

        <h4 className="mt-3 mb-1 text-xs font-semibold uppercase tracking-wide text-fg-subtle">Recent exchanges</h4>
        {recent.length === 0 ? (
          <p className="text-xs text-fg-muted">No exchanges yet.</p>
        ) : (
          <ul className="flex flex-col gap-1">
            {recent.map((c) => (
              <li key={c.id}>
                <button
                  type="button"
                  onClick={() => onPick(c)}
                  className="w-full rounded-md px-2 py-1.5 text-left text-xs hover:bg-surface-2"
                >
                  <span className="flex items-center gap-1.5">
                    <Badge tone={c.kind === "intercept" || c.kind === "approval" ? "warning" : c.kind === "correction" ? "danger" : c.kind === "protocol" ? "info" : c.kind === "user" ? "success" : "neutral"}>
                      {c.kind.replace("_", " ")}
                    </Badge>
                    <span className="min-w-0 flex-1 truncate text-fg">
                      {c.from.label} → {c.to.label}
                    </span>
                    <time className="shrink-0 tabular-nums text-fg-subtle" dateTime={c.ts}>{time(c.ts)}</time>
                  </span>
                  {c.label && <span className="mt-0.5 block truncate text-fg-muted">{c.label}</span>}
                  {c.ledger && (
                    <span className="mt-0.5 block truncate font-mono text-2xs text-info">
                      ledger #{c.ledger.id} · {c.ledger.hash.slice(0, 8)} · {c.ledger.method} · {Math.round(c.ledger.latencyMs)}ms
                    </span>
                  )}
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </aside>
  );
}

/* -------------------------------------------------------------------- legend */

function Legend() {
  const kinds = ["member", "overseer", "user", "tool", "mcp", "remote", "subcouncil"] as const;
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-fg-muted" aria-label="Legend" role="group">
      <span className="font-medium text-fg-subtle">Legend</span>
      {kinds.map((k) => {
        const g = glyphFor({ kind: k, role: k === "member" ? "researcher" : undefined });
        return (
          <span key={k} className="inline-flex items-center gap-1">
            <g.icon className={cn("size-3.5", g.fg)} aria-hidden />
            {KIND_LABEL[k]}
          </span>
        );
      })}
      <span className="inline-flex items-center gap-1">
        <svg width="12" height="12" aria-hidden focusable="false"><circle cx="6" cy="6" r="4" className="fill-accent/30 stroke-accent" /></svg>
        Glow = active
      </span>
      <span className="inline-flex items-center gap-1">
        <svg width="12" height="12" aria-hidden focusable="false"><circle cx="6" cy="6" r="3" className="fill-accent" /></svg>
        Pulse = message in flight
      </span>
      <span>Thicker conduit = more traffic</span>
      <span className="inline-flex items-center gap-1">
        <Link2 className="size-3.5 text-info" aria-hidden /> LDG = protocol ledger entry
      </span>
    </div>
  );
}
