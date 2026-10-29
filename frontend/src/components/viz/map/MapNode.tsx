import { Cpu, Eye, Globe, Plug, User, Users, Wrench, type LucideIcon } from "lucide-react";
import { memo, type KeyboardEvent } from "react";

import { roleStyle } from "@/components/ui/role";
import { cn } from "@/lib/cn";
import type { CommEvent, CommGraph, NodeKind, SubCouncilInfo } from "@/features/viz/types";

import { rhombus, sides, type CardPlacement, type Placed } from "./layout";
import { KIND_LABEL, STATUS_TONE } from "./style";

export type GraphNode = CommGraph["nodes"][number];
export type Ledger = NonNullable<CommEvent["ledger"]>;

const KIND_ICON: Partial<Record<NodeKind, LucideIcon>> = {
  user: User, tool: Wrench, mcp: Plug, remote: Globe, subcouncil: Users, system: Cpu,
};

export function glyphFor(n: Pick<GraphNode, "kind" | "role">): { icon: LucideIcon; fg: string } {
  if (n.kind === "member" || n.kind === "submember" || n.kind === "overseer") {
    const s = roleStyle(n.role);
    return { icon: n.kind === "overseer" && !n.role ? Eye : s.icon, fg: s.fg };
  }
  if (n.kind === "system" && n.role === "chair") return { icon: roleStyle("chair").icon, fg: roleStyle("chair").fg };
  if (n.kind === "user") return { icon: User, fg: roleStyle("user").fg };
  return { icon: KIND_ICON[n.kind] ?? Cpu, fg: "text-fg-muted" };
}

export function clip(s: string, n: number) {
  return s.length > n ? `${s.slice(0, n - 1)}…` : s;
}

export function money(usd: number) {
  return `$${usd < 0.1 ? usd.toFixed(4) : usd.toFixed(2)}`;
}

export function statusLabel(status?: string) {
  if (!status) return undefined;
  return STATUS_TONE[status]?.label ?? status.replace(/_/g, " ");
}

export interface NodeInfo {
  node: GraphNode;
  cost?: number;
  ledger?: Ledger;
  sub?: SubCouncilInfo;
}

export function describeNode({ node, cost, ledger, sub }: NodeInfo): string {
  const parts = [node.label, node.role && node.kind !== "subcouncil" ? `${node.role} ${KIND_LABEL[node.kind].toLowerCase()}` : KIND_LABEL[node.kind]];
  const st = statusLabel(node.status);
  if (st) parts.push(`status ${st}`);
  if (node.active) parts.push("active");
  parts.push(`${node.msgs} ${node.msgs === 1 ? "message" : "messages"}`);
  if (cost != null) parts.push(`cost ${money(cost)}`);
  if (ledger) parts.push(`last ${ledger.protocol} ${ledger.method}, ${Math.round(ledger.latencyMs)} ms, ledger ${ledger.hash.slice(0, 8)}`);
  if (sub) parts.push(`${sub.mode}, depth ${sub.depth}`);
  return parts.join(", ");
}

export const CARD_W = 146;
const LINE = 14;
const CARD_HEAD = 22;

export interface CardLine { text: string; cls: string }

/** Detail lines under the card title (empty for a name-only card). */
export function cardLines({ node, cost, ledger, sub }: NodeInfo): CardLine[] {
  const lines: CardLine[] = [];
  const st = statusLabel(node.status);
  lines.push({ text: `${st ?? KIND_LABEL[node.kind].toLowerCase()} · ${node.msgs} msg${node.msgs === 1 ? "" : "s"}`, cls: "fill-fg-muted" });
  if (cost != null) lines.push({ text: `${money(cost)}${node.role ? ` · ${node.role}` : ""}`, cls: "fill-fg-subtle" });
  else if (node.role && node.kind !== "subcouncil" && node.kind !== "remote") lines.push({ text: node.role, cls: "fill-fg-subtle" });
  if (sub) lines.push({ text: clip(`${sub.mode} · depth ${sub.depth}`, 24), cls: "fill-fg-subtle" });
  if (ledger) {
    lines.push({ text: clip(`${ledger.protocol} ${ledger.method}`, 24), cls: "fill-info" });
    lines.push({ text: `${Math.round(ledger.latencyMs)}ms · #${ledger.hash.slice(0, 8)}`, cls: "fill-info" });
  }
  return lines;
}

/** Card sizes: full (with `lineCount` detail lines) and the name-only fallback. */
export function cardSize(label: string, lineCount: number) {
  return {
    w: CARD_W,
    h: CARD_HEAD + lineCount * LINE,
    cw: Math.min(CARD_W, 30 + clip(label, 16).length * 6.2),
    ch: CARD_HEAD,
  };
}

interface Props extends NodeInfo {
  p: Placed;
  compact: boolean;
  selected: boolean;
  focused: boolean;
  highlight: boolean;
  reduced: boolean;
  zone?: number;
  /** Collision-free card position from `placeCards`. */
  card: CardPlacement;
  /** Non-interactive copy drawn on top so an emphasised card is never occluded (DOM/tab order stays put). */
  overlay?: boolean;
  onActivate: (id: string) => void;
  onFocusChange: (id: string | null) => void;
  onHover: (id: string | null) => void;
}

/** One platform + its floating card. Rendered as a single keyboard-focusable button. */
export const MapNode = memo(function MapNode(props: Props) {
  const { p, node, cost, ledger, sub, card, compact, selected, focused, highlight, reduced, zone, overlay, onActivate, onFocusChange, onHover } = props;
  const s = p.size;
  const z0 = p.z;
  const z1 = p.z + p.h;
  const [left, right] = sides(p.gx, p.gy, s, z0, z1);
  const top = rhombus(p.gx, p.gy, s, z1);
  const glyph = glyphFor(node);
  const Icon = glyph.icon;
  const lit = node.active || highlight;
  const emphasised = selected || focused || highlight;
  const tone = (node.status && STATUS_TONE[node.status]?.fill) || "fill-fg-subtle";

  // The placed card is collision-free; an emphasised overlay copy may expand a name-only card in
  // place (drawn on top, so it never hides behind a neighbour).
  const nameOnly = !(overlay && emphasised) && (card.compact || (compact && !emphasised));
  const lines = nameOnly ? [] : cardLines({ node, cost, ledger, sub });
  const size = cardSize(node.label, lines.length);
  const cardW = nameOnly ? card.w : size.w;
  const cardH = nameOnly ? card.h : size.h;
  const cx = card.anchor === "left" || card.anchor === "upleft" ? card.x + card.w - cardW
    : card.anchor === "above" || card.anchor === "below" ? card.x + (card.w - cardW) / 2 : card.x;
  const cy = card.anchor === "below" ? card.y : card.y + card.h - cardH;
  const lx = Math.min(cx + cardW, Math.max(cx, p.sx));
  const ly = Math.min(cy + cardH, Math.max(cy, p.sy - 4));

  const onKey = (e: KeyboardEvent<SVGGElement>) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      onActivate(node.id);
    }
  };

  return (
    <g
      {...(overlay
        ? { "aria-hidden": true, className: "pointer-events-none" }
        : {
            "data-node": node.id,
            "data-kind": node.kind,
            role: "button",
            tabIndex: 0,
            "aria-label": describeNode({ node, cost, ledger, sub }),
            "aria-current": selected ? ("true" as const) : undefined,
            className: "cursor-pointer outline-none",
            onClick: () => onActivate(node.id),
            onKeyDown: onKey,
            onFocus: () => onFocusChange(node.id),
            onBlur: () => onFocusChange(null),
            onPointerEnter: () => onHover(node.id),
            onPointerLeave: () => onHover(null),
          })}
    >
      {zone != null && !overlay && (
        <polygon points={rhombus(p.gx, p.gy, zone, z0)} className="fill-accent/5 stroke-accent/40" strokeDasharray="5 4" strokeWidth={1} />
      )}
      {lit && (
        <polygon
          points={rhombus(p.gx, p.gy, s + 0.28, z0)}
          className={cn("fill-accent/25 stroke-accent", !reduced && "animate-pulse")}
          strokeWidth={1.5}
          filter="url(#cm-glow)"
          data-glow={overlay ? undefined : ""}
        />
      )}
      <polygon points={left} className="fill-surface-3 stroke-border" strokeWidth={1} strokeLinejoin="round" />
      <polygon points={right} className="fill-surface-2 stroke-border" strokeWidth={1} strokeLinejoin="round" />
      <polygon
        points={top}
        className={cn("fill-surface", lit || selected ? "stroke-accent" : "stroke-border-strong")}
        strokeWidth={selected ? 2.5 : lit ? 1.75 : 1}
        strokeLinejoin="round"
      />
      {node.kind === "overseer" && (
        <polygon points={rhombus(p.gx, p.gy, s * 0.72, z1)} className="fill-none stroke-fg-subtle" strokeDasharray="3 2" strokeWidth={1} />
      )}
      <Icon x={p.sx - 7} y={p.sy - 7} width={14} height={14} className={glyph.fg} aria-hidden focusable="false" />
      {(focused || selected) && (
        <polygon points={rhombus(p.gx, p.gy, s + 0.42, z1)} className="fill-none stroke-accent" strokeWidth={2} strokeDasharray={focused ? "4 3" : undefined} />
      )}

      {/* Leader + floating card */}
      <line x1={p.sx} y1={p.sy - 4} x2={lx} y2={ly} className="stroke-border-strong" strokeWidth={1} />
      <g transform={`translate(${cx.toFixed(1)} ${cy.toFixed(1)})`}>
        <rect
          width={cardW}
          height={cardH}
          rx={6}
          className={cn("fill-surface", emphasised ? "stroke-accent" : lit ? "stroke-accent/60" : "stroke-border")}
          strokeWidth={emphasised ? 1.5 : 1}
          fillOpacity={0.94}
        />
        <Icon x={6} y={4} width={12} height={12} className={glyph.fg} aria-hidden focusable="false" />
        <text x={22} y={14} className="fill-fg font-sans" fontSize={11} fontWeight={600}>
          {clip(node.label, nameOnly ? 16 : 19)}
        </text>
        {lines.map((l, i) => (
          <text key={i} x={8} y={30 + i * LINE} className={cn(l.cls, "font-sans")} fontSize={10}>
            {i === 0 && <tspan className={tone}>● </tspan>}
            {l.text}
          </text>
        ))}
      </g>
    </g>
  );
});
