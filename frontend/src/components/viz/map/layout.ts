// Deterministic isometric layout for the council map. Pure functions: same node set → same picture,
// so the map never "jumps" as traffic flows. Positions are in grid units; `iso` projects to SVG px.

import type { NodeKind } from "@/features/viz/types";

/** Px per grid unit (2:1 isometric). */
export const UNIT = 30;

export interface LayoutInput {
  id: string;
  kind: NodeKind;
  role?: string;
  parent?: string;
}

export interface Placed {
  id: string;
  kind: NodeKind;
  gx: number;
  gy: number;
  /** Elevation of the platform base in px (overseer tier is raised). */
  z: number;
  /** Half-size of the platform's top face in grid units. */
  size: number;
  /** Platform thickness in px. */
  h: number;
  /** Screen position of the top-face centre. */
  sx: number;
  sy: number;
  depth: number;
}

export interface Layout {
  nodes: Map<string, Placed>;
  /** viewBox: [x, y, w, h] */
  box: [number, number, number, number];
  /** Grid extent for the floor. */
  extent: number;
}

export function iso(gx: number, gy: number, z = 0): [number, number] {
  return [(gx - gy) * UNIT, ((gx + gy) * UNIT) / 2 - z];
}

const SIZE: Record<NodeKind, number> = {
  user: 0.85, member: 1, overseer: 0.85, tool: 0.7, mcp: 0.75, remote: 0.8,
  subcouncil: 0.9, submember: 0.45, system: 0.7,
};
const HEIGHT: Record<NodeKind, number> = {
  user: 10, member: 12, overseer: 10, tool: 8, mcp: 10, remote: 10, subcouncil: 12, submember: 6, system: 8,
};
const OVERSEER_LIFT = 34;

const PERIPHERAL: ReadonlySet<NodeKind> = new Set(["tool", "mcp", "remote", "system"]);
const KIND_ORDER: Record<string, number> = { remote: 0, mcp: 1, tool: 2, system: 3 };

function byId(a: LayoutInput, b: LayoutInput) {
  return a.id < b.id ? -1 : a.id > b.id ? 1 : 0;
}

/** Stable key describing the node set; recompute layout only when this changes. */
export function layoutKey(nodes: LayoutInput[]): string {
  return nodes.map((n) => `${n.kind}:${n.id}:${n.role ?? ""}:${n.parent ?? ""}`).sort().join("|");
}

export function computeLayout(input: LayoutInput[]): Layout {
  const pos = new Map<string, { gx: number; gy: number; z: number; kind: NodeKind; size?: number }>();
  const nodes = [...input].sort(byId);

  const subIds = new Set(nodes.filter((n) => n.kind === "subcouncil").map((n) => n.id));
  // Anything owned by a sub-council (its members, its tools) clusters around that sub-council.
  const clustered = (n: LayoutInput) => n.kind === "submember" || (n.kind !== "subcouncil" && !!n.parent && subIds.has(n.parent));
  const top = nodes.filter((n) => !clustered(n));

  const members = top.filter((n) => n.kind === "member");
  const centre =
    members.find((m) => m.role === "chair") ??
    top.find((n) => n.kind === "system" && (n.role === "chair" || n.id === "council" || n.id === "chair"));
  const ring = members.filter((m) => m !== centre);
  const overseers = top.filter((n) => n.kind === "overseer");
  const peripherals = top
    .filter((n) => PERIPHERAL.has(n.kind) && n !== centre)
    .sort((a, b) => (KIND_ORDER[a.kind] ?? 9) - (KIND_ORDER[b.kind] ?? 9) || byId(a, b));
  const users = top.filter((n) => n.kind === "user");
  const subs = nodes.filter((n) => n.kind === "subcouncil");

  // Ring radius grows with membership so platforms (~2 units wide) never touch.
  const r1 = Math.max(3.2, (ring.length * 2.3) / (2 * Math.PI));
  const r2 = r1 + Math.max(3.4, (peripherals.length * 1.9) / (2 * Math.PI) - r1 + 1.5);

  if (centre) pos.set(centre.id, { gx: 0, gy: 0, z: 0, kind: centre.kind, size: 1.45 });

  // Members: start at the back (top of screen) and go clockwise. Angle -3π/4 is "up" in iso.
  const bearing = new Map<string, { a: number; r: number }>();
  ring.forEach((m, i) => {
    const a = -0.75 * Math.PI + (2 * Math.PI * i) / Math.max(1, ring.length);
    bearing.set(m.id, { a, r: r1 });
    pos.set(m.id, { gx: r1 * Math.cos(a), gy: r1 * Math.sin(a), z: 0, kind: m.kind });
  });
  if (centre) bearing.set(centre.id, { a: -0.75 * Math.PI, r: 0 });

  // Overseers float on a raised tier above the inner ring, between the chair and the members.
  overseers.forEach((o, i) => {
    const a = -0.75 * Math.PI + Math.PI / Math.max(2, overseers.length) + (2 * Math.PI * i) / Math.max(1, overseers.length);
    const r = overseers.length === 1 && !centre ? 0 : r1 * 0.55;
    pos.set(o.id, { gx: r * Math.cos(a), gy: r * Math.sin(a), z: OVERSEER_LIFT, kind: o.kind });
    bearing.set(o.id, { a, r });
  });

  // The user stands at the front (bottom of screen, angle π/4); peripherals fill the rest of the
  // outer ring, leaving a gap around the user.
  const front = 0.25 * Math.PI;
  users.forEach((u, i) => {
    const a = front + (i - (users.length - 1) / 2) * 0.35;
    pos.set(u.id, { gx: r2 * Math.cos(a), gy: r2 * Math.sin(a), z: 0, kind: u.kind });
  });
  const gap = users.length ? 0.45 : 0;
  const span = 2 * Math.PI - 2 * gap;
  peripherals.forEach((p, i) => {
    const a = front + gap + (span * (i + 0.5)) / Math.max(1, peripherals.length);
    pos.set(p.id, { gx: r2 * Math.cos(a), gy: r2 * Math.sin(a), z: 0, kind: p.kind });
  });

  // Sub-councils sit beyond the outer ring on their delegating member's bearing (nested ones
  // further out on their parent's bearing); their members and tools cluster tightly around them.
  const r3 = r2 + 3.4;
  const perParent = new Map<string, number>();
  const pending = [...subs];
  for (let pass = 0; pending.length && pass <= subs.length; pass++) {
    for (let i = pending.length - 1; i >= 0; i--) {
      const s = pending[i]!;
      const parent = s.parent ? bearing.get(s.parent) : undefined;
      const parentIsSub = !!s.parent && subIds.has(s.parent);
      if (parentIsSub && !parent && pass < subs.length) continue; // wait for the parent sub-council
      pending.splice(i, 1);
      const k = perParent.get(s.parent ?? "") ?? 0;
      perParent.set(s.parent ?? "", k + 1);
      const base = parent?.a ?? front + Math.PI + (perParent.get("") ?? 0) * 0.6;
      const a = base + (k % 2 ? -1 : 1) * Math.ceil(k / 2) * 0.42;
      const r = (parentIsSub && parent ? parent.r + 4.6 : r3) + (k > 2 ? 2.4 : 0);
      bearing.set(s.id, { a, r });
      pos.set(s.id, { gx: r * Math.cos(a), gy: r * Math.sin(a), z: 0, kind: s.kind });
    }
  }
  const kids = new Map<string, LayoutInput[]>();
  for (const n of nodes) {
    if (!clustered(n)) continue;
    const key = n.parent && pos.has(n.parent) ? n.parent : "";
    kids.set(key, [...(kids.get(key) ?? []), n]);
  }
  let orphanR = r3 + 4;
  for (const [parent, list] of kids) {
    const c = parent ? pos.get(parent)! : { gx: orphanR, gy: -orphanR, z: 0 };
    if (!parent) orphanR += 2;
    const rr = 1.5 + Math.max(0, list.length - 4) * 0.24;
    list.forEach((sm, i) => {
      const a = -0.75 * Math.PI + (2 * Math.PI * i) / list.length;
      pos.set(sm.id, { gx: c.gx + rr * Math.cos(a), gy: c.gy + rr * Math.sin(a), z: 0, kind: sm.kind, size: Math.min(SIZE[sm.kind] ?? 0.5, 0.5) });
    });
  }

  // Any kinds the contract may add later: park them on a back row so nothing is dropped.
  let extra = 0;
  for (const n of nodes) {
    if (pos.has(n.id)) continue;
    pos.set(n.id, { gx: -r2 - 2, gy: -r2 + 2 + extra * 2, z: 0, kind: n.kind });
    extra++;
  }

  const placed = new Map<string, Placed>();
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity, extent = 4;
  for (const [id, p] of pos) {
    const size = p.size ?? SIZE[p.kind] ?? 0.8;
    const h = HEIGHT[p.kind] ?? 8;
    const [sx, sy] = iso(p.gx, p.gy, p.z + h);
    placed.set(id, { id, kind: p.kind, gx: p.gx, gy: p.gy, z: p.z, size, h, sx, sy, depth: p.gx + p.gy });
    extent = Math.max(extent, Math.abs(p.gx) + 2, Math.abs(p.gy) + 2);
    // Card sits up-right of the platform: reserve room for it in the bounds.
    minX = Math.min(minX, sx - size * UNIT - 8);
    maxX = Math.max(maxX, sx + size * UNIT * 0.9 + 160);
    minY = Math.min(minY, sy - 86);
    maxY = Math.max(maxY, sy + size * UNIT + 24 + p.z);
  }
  if (!placed.size) return { nodes: placed, box: [-200, -120, 400, 240], extent };
  const pad = 16;
  return { nodes: placed, box: [minX - pad, minY - pad, maxX - minX + 2 * pad, maxY - minY + 2 * pad], extent: Math.ceil(extent) };
}

/** Right-angle conduit between two platforms, routed along iso axes on the floor. */
export function route(a: Placed, b: Placed): [number, number][] {
  // Pick the elbow deterministically from the pair so both directions share one conduit.
  const flip = (a.id < b.id ? a.id + b.id : b.id + a.id).length % 2 === 0;
  const [p, q] = a.id < b.id ? [a, b] : [b, a];
  const elbow: [number, number] = flip ? [q.gx, p.gy] : [p.gx, q.gy];
  const pts: [number, number][] = [];
  pts.push(iso(p.gx, p.gy, p.z + p.h));
  pts.push(iso(p.gx, p.gy, 0)); // riser, hidden inside the platform
  pts.push(iso(elbow[0], elbow[1], 0));
  pts.push(iso(q.gx, q.gy, 0));
  pts.push(iso(q.gx, q.gy, q.z + q.h));
  return a.id < b.id ? pts : pts.reverse();
}

export interface Polyline {
  pts: [number, number][];
  cum: number[];
  len: number;
}

export function polyline(pts: [number, number][]): Polyline {
  const cum = [0];
  for (let i = 1; i < pts.length; i++) {
    const [x0, y0] = pts[i - 1]!;
    const [x1, y1] = pts[i]!;
    cum.push(cum[i - 1]! + Math.hypot(x1 - x0, y1 - y0));
  }
  return { pts, cum, len: cum[cum.length - 1] ?? 0 };
}

export function pointAt(pl: Polyline, t: number): [number, number] {
  const d = Math.max(0, Math.min(1, t)) * pl.len;
  for (let i = 1; i < pl.pts.length; i++) {
    if (pl.cum[i]! >= d) {
      const seg = pl.cum[i]! - pl.cum[i - 1]! || 1;
      const k = (d - pl.cum[i - 1]!) / seg;
      const [x0, y0] = pl.pts[i - 1]!;
      const [x1, y1] = pl.pts[i]!;
      return [x0 + (x1 - x0) * k, y0 + (y1 - y0) * k];
    }
  }
  return pl.pts[pl.pts.length - 1] ?? [0, 0];
}

export function toPath(pts: [number, number][]): string {
  return pts.map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(1)} ${y.toFixed(1)}`).join(" ");
}

/** Four corners of a platform face at elevation z (N, E, S, W in screen terms). */
export function rhombus(gx: number, gy: number, s: number, z: number): string {
  return [iso(gx - s, gy - s, z), iso(gx + s, gy - s, z), iso(gx + s, gy + s, z), iso(gx - s, gy + s, z)]
    .map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`)
    .join(" ");
}

/** Visible side faces (front-left and front-right) of a platform. */
export function sides(gx: number, gy: number, s: number, z0: number, z1: number): [string, string] {
  const f = (pts: [number, number][]) => pts.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
  const left = f([iso(gx - s, gy + s, z1), iso(gx + s, gy + s, z1), iso(gx + s, gy + s, z0), iso(gx - s, gy + s, z0)]);
  const right = f([iso(gx + s, gy + s, z1), iso(gx + s, gy - s, z1), iso(gx + s, gy - s, z0), iso(gx + s, gy + s, z0)]);
  return [left, right];
}
