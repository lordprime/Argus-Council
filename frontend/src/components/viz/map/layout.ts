// Deterministic isometric layout for the council map. Pure functions: same node set → same picture,
// so the map never "jumps" as traffic flows. Positions are in grid units; `iso` projects to SVG px.
//
// Each group of agents gets its own board, and the groups sit on separate levels: overseers on the
// top board, the council on the middle one, tools / MCP servers / remote agents on the ground. The
// user has a small board beside the council; every sub-council gets a board of its own next to it.
// Inside a board platforms sit on a grid whose gutters (halfway between rows and columns) carry the
// conduits, so links never cut across a platform. `routeLinks` routes every link once: in gutters on
// a board, through the gap between neighbouring boards (with a riser where the level changes), or
// along a side spine for boards that are not neighbours. Links that share a gutter get their own lane.

import type { NodeKind } from "@/features/viz/types";

/** Px per grid unit (2:1 isometric). */
export const UNIT = 30;
/** Height between levels, px. */
export const LEVEL = 60;
/** Thickness of a board slab, px. */
export const BOARD_THICKNESS = 7;
/** Grid units between neighbouring boards. */
const BOARD_GAP = 2.6;
/** Spacing of the left spine from the boards it runs past. */
const SPINE_MARGIN = 1.4;
/** Grid units between parallel conduits in one gutter. */
const LANE = 0.24;

export interface LayoutInput {
  id: string;
  kind: NodeKind;
  role?: string;
  parent?: string;
}

export type BoardKind = "oversight" | "council" | "tools" | "user" | "sub" | "detached";

export interface Board {
  id: string;
  kind: BoardKind;
  label: string;
  /** The sub-council node a "sub" board belongs to. */
  hub?: string;
  /** Footprint in grid units. */
  x0: number;
  y0: number;
  x1: number;
  y1: number;
  /** Elevation of the top face in px. */
  z: number;
  /** Grid spacing between platforms; gutters run at half this from each platform. */
  step: number;
}

export interface Placed {
  id: string;
  kind: NodeKind;
  gx: number;
  gy: number;
  /** Elevation of the platform base in px (the level of its board). */
  z: number;
  /** Half-size of the platform's top face in grid units. */
  size: number;
  /** Platform thickness in px. */
  h: number;
  /** Screen position of the top-face centre. */
  sx: number;
  sy: number;
  depth: number;
  /** Id of the board the platform stands on. */
  board: string;
}

export interface Layout {
  nodes: Map<string, Placed>;
  boards: Board[];
  /** viewBox: [x, y, w, h] */
  box: [number, number, number, number];
  /** Grid extent (max |coordinate|), for anything that wants a floor. */
  extent: number;
  /** x of the spines left and right of the boards (conduits between non-neighbouring boards). */
  spine: { left: number; right: number };
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
const CHAIR_SIZE = 1.45;

const PERIPHERAL: ReadonlySet<NodeKind> = new Set(["tool", "mcp", "remote", "system"]);
const KIND_ORDER: Record<string, number> = { remote: 0, mcp: 1, tool: 2, system: 3 };

const LABEL: Record<BoardKind, string> = {
  oversight: "Oversight", council: "Council", tools: "Tools & connections", user: "You", sub: "Sub-council", detached: "Detached",
};

function byId(a: LayoutInput, b: LayoutInput) {
  return a.id < b.id ? -1 : a.id > b.id ? 1 : 0;
}

/** Stable key describing the node set; recompute layout only when this changes. */
export function layoutKey(nodes: LayoutInput[]): string {
  return nodes.map((n) => `${n.kind}:${n.id}:${n.role ?? ""}:${n.parent ?? ""}`).sort().join("|");
}

interface Group {
  kind: BoardKind;
  id: string;
  hub?: string;
  nodes: LayoutInput[];
  step: number;
  maxCols: number;
  z: number;
  /** Size overrides (chair, sub-council members). */
  sizes?: Map<string, number>;
}

/** Grid cells for n platforms, best spot first: the front row (towards the viewer), centre outwards. */
function cells(n: number, maxCols: number): { cols: number; rows: number; order: [number, number][] } {
  const cols = Math.max(1, Math.min(maxCols, Math.ceil(Math.sqrt(n * 2))));
  const rows = Math.max(1, Math.ceil(n / cols));
  const mid = (cols - 1) / 2;
  const order: [number, number][] = [];
  for (let r = rows - 1; r >= 0; r--) {
    const row = Array.from({ length: cols }, (_, c) => c).sort((a, b) => Math.abs(a - mid) - Math.abs(b - mid) || a - b);
    for (const c of row) order.push([c, r]);
  }
  return { cols, rows, order };
}

export function computeLayout(input: LayoutInput[]): Layout {
  const nodes = [...input].sort(byId);
  const subIds = new Set(nodes.filter((n) => n.kind === "subcouncil").map((n) => n.id));
  // Anything owned by a sub-council (its members, its tools) goes on that sub-council's board.
  const clustered = (n: LayoutInput) => n.kind === "submember" || (n.kind !== "subcouncil" && !!n.parent && subIds.has(n.parent));
  const top = nodes.filter((n) => !clustered(n) && n.kind !== "subcouncil");

  const members = top.filter((n) => n.kind === "member");
  const centre =
    members.find((m) => m.role === "chair") ??
    top.find((n) => n.kind === "system" && (n.role === "chair" || n.id === "council" || n.id === "chair"));
  const council = [...(centre ? [centre] : []), ...members.filter((m) => m !== centre)];
  const overseers = top.filter((n) => n.kind === "overseer");
  const users = top.filter((n) => n.kind === "user");
  const known = new Set([...council, ...overseers, ...users].map((n) => n.id));
  // Tools and connections, plus any kind the contract may add later (so nothing is dropped).
  const peripherals = top
    .filter((n) => !known.has(n.id))
    .sort((a, b) => (PERIPHERAL.has(a.kind) ? KIND_ORDER[a.kind] ?? 9 : 10) - (PERIPHERAL.has(b.kind) ? KIND_ORDER[b.kind] ?? 9 : 10) || byId(a, b));

  // The level stack, back (top of screen, highest) to front (ground).
  const stack: Group[] = [];
  if (overseers.length) stack.push({ kind: "oversight", id: "board:oversight", nodes: overseers, step: 4, maxCols: 6, z: 2 * LEVEL });
  if (council.length) {
    stack.push({
      kind: "council", id: "board:council", nodes: council, step: 4.4, maxCols: 5, z: LEVEL,
      sizes: centre ? new Map([[centre.id, CHAIR_SIZE]]) : undefined,
    });
  }
  if (peripherals.length) stack.push({ kind: "tools", id: "board:tools", nodes: peripherals, step: 3.6, maxCols: 7, z: 0 });

  const boards: Board[] = [];
  const pos = new Map<string, { gx: number; gy: number; z: number; kind: NodeKind; size?: number; board: string }>();
  const put = (g: Group, x0: number, y0: number) => {
    const { cols, rows, order } = cells(g.nodes.length, g.maxCols);
    const board: Board = {
      id: g.id, kind: g.kind, label: LABEL[g.kind], hub: g.hub,
      x0, y0, x1: x0 + cols * g.step, y1: y0 + rows * g.step, z: g.z, step: g.step,
    };
    boards.push(board);
    g.nodes.forEach((n, i) => {
      const [c, r] = order[i]!;
      pos.set(n.id, {
        gx: x0 + (c + 0.5) * g.step, gy: y0 + (r + 0.5) * g.step, z: g.z, kind: n.kind,
        size: g.sizes?.get(n.id), board: g.id,
      });
    });
    return board;
  };

  // Stack boards along gy, centred on gx = 0, each separated by a gap.
  let cursor = 0;
  for (const g of stack) {
    const { cols } = cells(g.nodes.length, g.maxCols);
    const b = put(g, -(cols * g.step) / 2, cursor);
    cursor = b.y1 + BOARD_GAP;
  }
  const councilBoard = boards.find((b) => b.kind === "council") ?? boards[0];
  const stackX0 = Math.min(0, ...boards.map((b) => b.x0));
  let stackX1 = Math.max(0, ...boards.map((b) => b.x1));

  // The user's board sits left of the council, with the left spine running between them.
  if (users.length) {
    const g: Group = { kind: "user", id: "board:user", nodes: users, step: 3, maxCols: 2, z: councilBoard?.z ?? 0 };
    const { cols, rows } = cells(users.length, g.maxCols);
    const cy = councilBoard ? (councilBoard.y0 + councilBoard.y1) / 2 : 0;
    put(g, stackX0 - 2 * SPINE_MARGIN - cols * g.step, cy - (rows * g.step) / 2);
  }

  // Sub-councils: one board each, in columns to the right of the council (nested ones further right).
  const subs = nodes.filter((n) => n.kind === "subcouncil");
  const kids = new Map<string, LayoutInput[]>();
  const orphans: LayoutInput[] = [];
  for (const n of nodes) {
    if (!clustered(n)) continue;
    if (n.parent && subIds.has(n.parent)) kids.set(n.parent, [...(kids.get(n.parent) ?? []), n]);
    else orphans.push(n);
  }
  const boardOf = (id: string | undefined) => (id ? boards.find((b) => b.id === pos.get(id)?.board) : undefined);
  const columnCursor = new Map<string, number>();
  const pending = [...subs];
  for (let pass = 0; pending.length && pass <= subs.length; pass++) {
    for (let i = 0; i < pending.length; ) {
      const s = pending[i]!;
      const parentIsSub = !!s.parent && subIds.has(s.parent);
      if (parentIsSub && !pos.has(s.parent!) && pass < subs.length) { i++; continue; } // wait for the parent
      pending.splice(i, 1);
      const parent = boardOf(s.parent) ?? councilBoard;
      const x0 = (parent ? parent.x1 : stackX1) + BOARD_GAP;
      const key = x0.toFixed(2);
      const y0 = columnCursor.get(key) ?? (parent?.y0 ?? 0);
      const list = kids.get(s.id) ?? [];
      const sizes = new Map(list.map((k) => [k.id, Math.min(SIZE[k.kind] ?? 0.5, 0.5)]));
      const b = put({ kind: "sub", id: `board:${s.id}`, hub: s.id, nodes: [s, ...list], step: 3, maxCols: 4, z: LEVEL, sizes }, x0, y0);
      columnCursor.set(key, b.y1 + BOARD_GAP);
    }
  }
  stackX1 = Math.max(stackX1, ...boards.map((b) => b.x1));
  if (orphans.length) {
    const sizes = new Map(orphans.map((k) => [k.id, Math.min(SIZE[k.kind] ?? 0.5, 0.5)]));
    put({ kind: "detached", id: "board:detached", nodes: orphans, step: 3, maxCols: 4, z: 0, sizes }, stackX1 + BOARD_GAP, cursor);
  }

  const placed = new Map<string, Placed>();
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity, extent = 4;
  for (const [id, p] of pos) {
    const size = p.size ?? SIZE[p.kind] ?? 0.8;
    const h = HEIGHT[p.kind] ?? 8;
    const [sx, sy] = iso(p.gx, p.gy, p.z + h);
    placed.set(id, { id, kind: p.kind, gx: p.gx, gy: p.gy, z: p.z, size, h, sx, sy, depth: p.gx + p.gy - p.z / UNIT, board: p.board });
    // Card sits up-right of the platform: reserve room for it in the bounds.
    minX = Math.min(minX, sx - size * UNIT - 8);
    maxX = Math.max(maxX, sx + size * UNIT * 0.9 + 160);
    minY = Math.min(minY, sy - 86);
    maxY = Math.max(maxY, sy + size * UNIT + 24);
  }
  for (const b of boards) {
    for (const [x, y] of [iso(b.x0, b.y0, b.z), iso(b.x1, b.y0, b.z), iso(b.x1, b.y1, b.z), iso(b.x0, b.y1, b.z - BOARD_THICKNESS)]) {
      minX = Math.min(minX, x - 4); maxX = Math.max(maxX, x + 4);
      minY = Math.min(minY, y - 18); maxY = Math.max(maxY, y + BOARD_THICKNESS + 4);
    }
    extent = Math.max(extent, Math.abs(b.x0), Math.abs(b.x1), Math.abs(b.y0), Math.abs(b.y1));
  }
  const spine = {
    left: Math.min(0, ...boards.filter((b) => b.kind !== "user").map((b) => b.x0)) - SPINE_MARGIN,
    right: Math.max(0, ...boards.map((b) => b.x1)) + SPINE_MARGIN,
  };
  if (!placed.size) return { nodes: placed, boards, box: [-200, -120, 400, 240], extent, spine };
  const pad = 16;
  return {
    nodes: placed, boards, spine,
    box: [minX - pad, minY - pad, maxX - minX + 2 * pad, maxY - minY + 2 * pad],
    extent: Math.ceil(extent + 1),
  };
}

/* ------------------------------------------------------------------ routing */

type P3 = [number, number, number];

/** Key of an unordered node pair; routes are stored from the smaller id to the larger. */
export function routeKey(a: string, b: string): string {
  return a < b ? `${a}\n${b}` : `${b}\n${a}`;
}

function laneOffset(i: number, max: number): number {
  const k = Math.ceil(i / 2) * (i % 2 ? -1 : 1) * LANE;
  if (max <= 0) return 0;
  const cap = Math.max(1, Math.floor(max / LANE));
  return Math.abs(k) <= max ? k : (((Math.ceil(i / 2) - 1) % cap) + 1) * (i % 2 ? -1 : 1) * LANE;
}

function overlaps(a0: number, a1: number, b0: number, b1: number) {
  return Math.min(a1, b1) - Math.max(a0, b0) > 0.01;
}

/**
 * Route every link of a layout. Deterministic: links are routed in key order, each taking the next free
 * lane of every gutter, gap and spine it uses, and the next port on both platforms.
 */
export function routeLinks(layout: Layout, pairs: Iterable<[string, string]>): Map<string, [number, number][]> {
  const boards = new Map(layout.boards.map((b) => [b.id, b]));
  const keys = [...new Set([...pairs].filter(([a, b]) => a !== b && layout.nodes.has(a) && layout.nodes.has(b)).map(([a, b]) => routeKey(a, b)))].sort();
  const lanes = new Map<string, number>();
  const take = (channel: string, max: number) => {
    const i = lanes.get(channel) ?? 0;
    lanes.set(channel, i + 1);
    return laneOffset(i, max);
  };
  const ports = new Map<string, number>();
  const port = (p: Placed) => {
    const i = ports.get(p.id) ?? 0;
    ports.set(p.id, i + 1);
    return laneOffset(i, p.size * 0.6);
  };
  // Lanes must clear the largest platform on the board.
  const largest = new Map<string, number>();
  for (const p of layout.nodes.values()) largest.set(p.board, Math.max(largest.get(p.board) ?? 0, p.size));
  const gutterMax = (b: Board) => Math.max(0, b.step / 2 - (largest.get(b.id) ?? 1) - 0.12);
  const out = new Map<string, [number, number][]>();

  for (const key of keys) {
    const [ia, ib] = key.split("\n") as [string, string];
    const a = layout.nodes.get(ia)!;
    const b = layout.nodes.get(ib)!;
    const ba = boards.get(a.board);
    const bb = boards.get(b.board);
    let path: P3[];
    if (!ba || !bb) {
      path = [[a.gx, a.gy, a.z], [b.gx, b.gy, b.z]];
    } else if (ba === bb) {
      path = sameBoard(a, b, ba, take, port, gutterMax(ba));
    } else if (ba.y1 <= bb.y0 + 1e-6 && overlaps(ba.x0, ba.x1, bb.x0, bb.x1) && !between(layout.boards, ba, bb, "y")) {
      path = acrossY(a, b, ba, bb, take, port, gutterMax);
    } else if (bb.y1 <= ba.y0 + 1e-6 && overlaps(ba.x0, ba.x1, bb.x0, bb.x1) && !between(layout.boards, bb, ba, "y")) {
      path = acrossY(b, a, bb, ba, take, port, gutterMax).reverse();
    } else if (ba.x1 <= bb.x0 + 1e-6 && overlaps(ba.y0, ba.y1, bb.y0, bb.y1) && !between(layout.boards, ba, bb, "x")) {
      path = viaColumn(a, b, ba, bb, (ba.x1 + bb.x0) / 2, `gx:${ba.id}:${bb.id}`, BOARD_GAP / 2 - 0.3, take, port, gutterMax);
    } else if (bb.x1 <= ba.x0 + 1e-6 && overlaps(ba.y0, ba.y1, bb.y0, bb.y1) && !between(layout.boards, bb, ba, "x")) {
      path = viaColumn(b, a, bb, ba, (bb.x1 + ba.x0) / 2, `gx:${bb.id}:${ba.id}`, BOARD_GAP / 2 - 0.3, take, port, gutterMax).reverse();
    } else {
      // Not neighbours: along the spine on the side of the boards involved.
      const right = ba.kind === "sub" || bb.kind === "sub" || ba.kind === "detached" || bb.kind === "detached";
      const x = right ? layout.spine.right : layout.spine.left;
      path = viaColumn(a, b, ba, bb, x, right ? "spine:right" : "spine:left", SPINE_MARGIN - 0.3, take, port, gutterMax);
    }
    const pts: [number, number][] = [iso(a.gx, a.gy, a.z + a.h)];
    for (const [x, y, z] of path) {
      const q = iso(x, y, z);
      const last = pts[pts.length - 1]!;
      if (Math.abs(q[0] - last[0]) > 0.05 || Math.abs(q[1] - last[1]) > 0.05) pts.push(q);
    }
    const end = iso(b.gx, b.gy, b.z + b.h);
    const last = pts[pts.length - 1]!;
    if (Math.abs(end[0] - last[0]) > 0.05 || Math.abs(end[1] - last[1]) > 0.05) pts.push(end);
    out.set(key, pts);
  }
  return out;
}

/** Is there a board strictly between `a` and `b` (a before b along the axis) overlapping both on the other axis? */
function between(all: Board[], a: Board, b: Board, axis: "x" | "y"): boolean {
  return all.some((c) => c !== a && c !== b && (axis === "y"
    ? c.y0 >= a.y1 - 1e-6 && c.y1 <= b.y0 + 1e-6 && overlaps(c.x0, c.x1, Math.max(a.x0, b.x0), Math.min(a.x1, b.x1))
    : c.x0 >= a.x1 - 1e-6 && c.x1 <= b.x0 + 1e-6 && overlaps(c.y0, c.y1, Math.max(a.y0, b.y0), Math.min(a.y1, b.y1))));
}

type Take = (channel: string, max: number) => number;
type Port = (p: Placed) => number;

/** Gutter line next to a platform on the side facing `toward` (ties go to the front / right). */
const gutter = (c: number, toward: number, step: number) => c + (toward < c - 1e-6 ? -1 : 1) * (step / 2);

function sameBoard(a: Placed, b: Placed, board: Board, take: Take, port: Port, max: number): P3[] {
  const z = board.z;
  const s = board.step;
  const pa = port(a);
  const pb = port(b);
  if (Math.abs(a.gy - b.gy) < 1e-6) {
    // Same row: along the gutter in front of it (behind it for every other pair, to spread the load).
    const y = gutter(a.gy, (a.id.length + b.id.length) % 2 ? a.gy - 1 : a.gy + 1, s);
    const o = take(`r:${board.id}:${y.toFixed(2)}`, max);
    return [[a.gx, a.gy, z], [a.gx + pa, a.gy, z], [a.gx + pa, y + o, z], [b.gx + pb, y + o, z], [b.gx + pb, b.gy, z], [b.gx, b.gy, z]];
  }
  if (Math.abs(a.gx - b.gx) < 1e-6) {
    const ya = gutter(a.gy, b.gy, s);
    const yb = gutter(b.gy, a.gy, s);
    if (Math.abs(ya - yb) < 1e-6) {
      // Neighbours in one column: straight across the gutter between them.
      return [[a.gx + pa, a.gy, z], [b.gx + pa, b.gy, z]];
    }
    const x = gutter(a.gx, (a.id.length + b.id.length) % 2 ? a.gx - 1 : a.gx + 1, s);
    const o = take(`c:${board.id}:${x.toFixed(2)}`, max);
    return [[a.gx, a.gy, z], [a.gx, a.gy + pa, z], [x + o, a.gy + pa, z], [x + o, b.gy + pb, z], [b.gx, b.gy + pb, z], [b.gx, b.gy, z]];
  }
  const ya = gutter(a.gy, b.gy, s);
  const yb = gutter(b.gy, a.gy, s);
  const oa = take(`r:${board.id}:${ya.toFixed(2)}`, max);
  if (Math.abs(ya - yb) < 1e-6) {
    return [[a.gx, a.gy, z], [a.gx + pa, a.gy, z], [a.gx + pa, ya + oa, z], [b.gx + pb, ya + oa, z], [b.gx + pb, b.gy, z], [b.gx, b.gy, z]];
  }
  const x = gutter(a.gx, b.gx, s);
  const oc = take(`c:${board.id}:${x.toFixed(2)}`, max);
  const ob = take(`r:${board.id}:${yb.toFixed(2)}`, max);
  return [
    [a.gx, a.gy, z], [a.gx + pa, a.gy, z], [a.gx + pa, ya + oa, z], [x + oc, ya + oa, z],
    [x + oc, yb + ob, z], [b.gx + pb, yb + ob, z], [b.gx + pb, b.gy, z], [b.gx, b.gy, z],
  ];
}

/** `a` on the board behind `b`'s: down a column gutter to the gap, riser to `b`'s level, across, in. */
function acrossY(a: Placed, b: Placed, ba: Board, bb: Board, take: Take, port: Port, max: (b: Board) => number): P3[] {
  const pa = port(a);
  const pb = port(b);
  const g = (ba.y1 + bb.y0) / 2 + take(`gy:${ba.id}:${bb.id}`, BOARD_GAP / 2 - 0.3);
  const ya = gutter(a.gy, a.gy + 1, ba.step);
  const yb = gutter(b.gy, b.gy - 1, bb.step);
  const oya = take(`r:${ba.id}:${ya.toFixed(2)}`, max(ba));
  const oyb = take(`r:${bb.id}:${yb.toFixed(2)}`, max(bb));
  const pts: P3[] = [[a.gx, a.gy, a.z], [a.gx + pa, a.gy, a.z], [a.gx + pa, ya + oya, a.z]];
  let xa = a.gx + pa;
  if (Math.abs(ya - ba.y1) > 1e-6) {
    // Not on the front row: reach the board edge along the column gutter on b's side.
    xa = gutter(a.gx, b.gx, ba.step) + take(`c:${ba.id}:${gutter(a.gx, b.gx, ba.step).toFixed(2)}`, max(ba));
    pts.push([xa, ya + oya, a.z]);
  }
  let xb = b.gx + pb;
  const backRow = Math.abs(yb - bb.y0) < 1e-6;
  if (!backRow) xb = gutter(b.gx, a.gx, bb.step) + take(`c:${bb.id}:${gutter(b.gx, a.gx, bb.step).toFixed(2)}`, max(bb));
  pts.push([xa, g, a.z], [xa, g, b.z], [xb, g, b.z], [xb, yb + oyb, b.z]);
  if (!backRow) pts.push([b.gx + pb, yb + oyb, b.z]);
  pts.push([b.gx + pb, b.gy, b.z], [b.gx, b.gy, b.z]);
  return pts;
}

/** Out along row gutters to the vertical line x (a gap or a spine), riser, along it, and in along b's row gutter. */
function viaColumn(
  a: Placed, b: Placed, ba: Board, bb: Board, x: number, channel: string, cap: number, take: Take, port: Port, max: (b: Board) => number,
): P3[] {
  const pa = port(a);
  const pb = port(b);
  const xo = x + take(channel, cap);
  const ya = gutter(a.gy, b.gy, ba.step);
  const yb = gutter(b.gy, a.gy, bb.step);
  const oya = take(`r:${ba.id}:${ya.toFixed(2)}`, max(ba));
  const oyb = take(`r:${bb.id}:${yb.toFixed(2)}`, max(bb));
  return [
    [a.gx, a.gy, a.z], [a.gx + pa, a.gy, a.z], [a.gx + pa, ya + oya, a.z], [xo, ya + oya, a.z],
    [xo, ya + oya, b.z], [xo, yb + oyb, b.z], [b.gx + pb, yb + oyb, b.z], [b.gx + pb, b.gy, b.z], [b.gx, b.gy, b.z],
  ];
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

/* ------------------------------------------------------------------ card placement */

export type CardAnchor = "upright" | "above" | "upleft" | "right" | "left" | "below";

export interface CardSpec {
  id: string;
  /** Full card size. */
  w: number;
  h: number;
  /** Name-only fallback size. */
  cw: number;
  ch: number;
  /** Lower = placed first (gets the best spot). */
  priority: number;
}

export interface CardPlacement {
  x: number;
  y: number;
  w: number;
  h: number;
  anchor: CardAnchor;
  /** True when the card had to shrink to its name-only form to avoid overlapping a neighbour. */
  compact: boolean;
  /** Leader line end (nearest point on the card to the platform). */
  lx: number;
  ly: number;
}

interface Rect { x: number; y: number; w: number; h: number }

const ANCHORS: CardAnchor[] = ["upright", "above", "upleft", "right", "left", "below"];
const CARD_GAP = 6;

function overlapArea(a: Rect, b: Rect): number {
  const w = Math.min(a.x + a.w, b.x + b.w) - Math.max(a.x, b.x);
  const h = Math.min(a.y + a.h, b.y + b.h) - Math.max(a.y, b.y);
  return w > 0 && h > 0 ? w * h : 0;
}

/** Screen-space box of a platform's visible body (top face + sides), slightly inset from the rhombus corners. */
export function platformRect(p: Placed): Rect {
  const hw = p.size * UNIT * 1.6;
  const hh = p.size * UNIT * 0.8;
  return { x: p.sx - hw, y: p.sy - hh, w: 2 * hw, h: 2 * hh + p.h };
}

function anchorAt(p: Placed, a: CardAnchor, w: number, h: number): Rect {
  const s = p.size * UNIT;
  switch (a) {
    case "upright": return { x: p.sx + s * 0.9 + 6, y: p.sy - 34 - h / 2, w, h };
    case "upleft": return { x: p.sx - s * 0.9 - 6 - w, y: p.sy - 34 - h / 2, w, h };
    case "above": return { x: p.sx - w / 2, y: p.sy - s - 12 - h, w, h };
    case "right": return { x: p.sx + s * 2 + 4, y: p.sy - h / 2, w, h };
    case "left": return { x: p.sx - s * 2 - 4 - w, y: p.sy - h / 2, w, h };
    case "below": return { x: p.sx - w / 2, y: p.sy + s + p.h + 8, w, h };
  }
}

/**
 * Greedy, deterministic card placement. Cards are placed in `priority` order; each tries the anchor
 * positions around its platform and keeps the one with the least overlap with cards already placed
 * (weighted heavily) and with other platforms. A card that still collides falls back to its
 * name-only size, and if that collides too it takes the least-bad compact spot.
 */
export function placeCards(layout: Layout, specs: CardSpec[]): Map<string, CardPlacement> {
  const out = new Map<string, CardPlacement>();
  const platforms = [...layout.nodes.values()].map((p) => ({ id: p.id, r: platformRect(p) }));
  const placed: Rect[] = [];
  const order = [...specs].filter((s) => layout.nodes.has(s.id))
    .sort((a, b) => a.priority - b.priority || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));

  const pad = (r: Rect): Rect => ({ x: r.x - CARD_GAP / 2, y: r.y - CARD_GAP / 2, w: r.w + CARD_GAP, h: r.h + CARD_GAP });
  const best = (p: Placed, w: number, h: number) => {
    let pick: { r: Rect; a: CardAnchor; cards: number; score: number } | null = null;
    ANCHORS.forEach((a, i) => {
      const r = anchorAt(p, a, w, h);
      const pr = pad(r);
      let cards = 0;
      for (const q of placed) cards += overlapArea(pr, q);
      let plats = 0;
      for (const q of platforms) plats += overlapArea(r, q.r) * (q.id === p.id ? 0.6 : 1);
      const score = cards * 10 + plats + i * 40; // small bias keeps the familiar up-right default
      if (!pick || score < pick.score) pick = { r, a, cards, score };
    });
    return pick!;
  };

  for (const s of order) {
    const p = layout.nodes.get(s.id)!;
    let choice = best(p, s.w, s.h);
    let compact = false;
    if (choice.cards > 0) {
      choice = best(p, s.cw, s.ch);
      compact = true;
    }
    const r = choice.r;
    placed.push(r);
    out.set(s.id, {
      ...r, anchor: choice.a, compact,
      lx: Math.min(r.x + r.w, Math.max(r.x, p.sx)),
      ly: Math.min(r.y + r.h, Math.max(r.y, p.sy - 4)),
    });
  }
  return out;
}

/** Bounding box [x, y, w, h] covering the layout box and every placed card. */
export function boxWithCards(layout: Layout, cards: Iterable<CardPlacement>): [number, number, number, number] {
  const [bx, by, bw, bh] = layout.box;
  let x0 = bx, y0 = by, x1 = bx + bw, y1 = by + bh;
  for (const c of cards) {
    x0 = Math.min(x0, c.x - 12); y0 = Math.min(y0, c.y - 12);
    x1 = Math.max(x1, c.x + c.w + 12); y1 = Math.max(y1, c.y + c.h + 12);
  }
  return [x0, y0, x1 - x0, y1 - y0];
}
