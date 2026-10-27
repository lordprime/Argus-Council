// Normalised communication events for the heatmap + route views. Prefers `run.comms` (populated by the
// reducer from the SSE stream + protocol ledger) and falls back to deriving from timeline items, so the
// views work against any RunState. Types are structural so this file has no hard dependency on the
// viz model module.

import type { Actor } from "@/lib/types";
import type { RunState, TimelineItem } from "@/stores/run";

export type CommKind =
  | "assign" | "thought" | "tool" | "tool_result" | "intercept" | "correction"
  | "approval" | "vote" | "user" | "protocol" | "subcouncil" | "status";

export interface NodeRefLike { kind: string; id: string; label: string; role?: string; parent?: string }

export interface CommEventLike {
  id: string;
  seq: number;
  ts: string;
  kind: CommKind;
  from: NodeRefLike;
  to: NodeRefLike;
  label?: string;
  status?: string;
  childRunId?: string;
  ledger?: { id: number | string; hash: string; protocol: string; latencyMs: number; method: string };
}

export interface SubCouncilLike { id: string; task: string; status: string; startedTs: string; endedTs?: string }

export type RunWithViz = RunState & { comms?: CommEventLike[]; subcouncils?: SubCouncilLike[] | Record<string, SubCouncilLike> };

/** One unit of activity attributed to a node at a point in time. */
export interface Activity {
  id: string;
  itemId?: string;
  t: number;
  node: NodeRefLike;
  kind: CommKind | "error";
  tokens: number;
  cost: number;
  error: boolean;
}

const ERROR_STATUS = new Set(["error", "failed", "blocked", "timeout", "rejected"]);

export function ms(ts: string | undefined): number {
  const t = ts ? Date.parse(ts) : NaN;
  return Number.isFinite(t) ? t : 0;
}

export function actorNode(a: Actor | undefined, run: RunState): NodeRefLike {
  if (!a) return { kind: "system", id: "system", label: "System" };
  if (a.kind === "user") return { kind: "user", id: "user", label: a.name ?? "You", role: "user" };
  const member = a.member_id ? run.members.find((m) => m.id === a.member_id) : undefined;
  const id = a.member_id ?? (a.name ? `${a.kind}:${a.name}` : a.kind);
  return {
    kind: a.kind === "remote" ? "remote" : a.overseer ? "overseer" : a.kind === "agent" ? "member" : "system",
    id,
    label: member?.name ?? a.name ?? (a.kind === "system" ? "System" : id),
    role: a.role ?? member?.role ?? (a.kind === "remote" ? "remote" : undefined),
  };
}

function itemKind(it: TimelineItem): CommKind | "error" {
  switch (it.kind) {
    case "thought": return "thought";
    case "tool": return "tool";
    case "approval": return "approval";
    case "vote": return "vote";
    case "user": return it.type === "correction.issued" ? "correction" : "user";
    case "error":
    case "violation": return "error";
    case "plan": return "assign";
    default:
      if (it.type.startsWith("intercept")) return "intercept";
      if (it.type.startsWith("correction")) return "correction";
      if (it.type.startsWith("subcouncil")) return "subcouncil";
      if (it.type.startsWith("protocol") || it.type.startsWith("remote")) return "protocol";
      return "status";
  }
}

export function orderedItems(run: RunState): TimelineItem[] {
  return run.order.map((id) => run.items[id]).filter((x): x is TimelineItem => !!x);
}

/** Activity per node, from `run.comms` when present, else derived from timeline items. */
export function deriveActivity(run: RunState): Activity[] {
  const comms = (run as RunWithViz).comms;
  if (comms && comms.length) {
    return comms.map((c) => {
      const item = run.items[c.id];
      const tokens = item?.kind === "thought" ? (item.tokensIn ?? 0) + (item.tokensOut ?? 0) : 0;
      const cost = item?.kind === "thought" ? item.costUsd ?? 0 : 0;
      return {
        id: c.id,
        itemId: item ? c.id : undefined,
        t: ms(c.ts),
        node: c.from,
        kind: c.kind,
        tokens,
        cost,
        error: (c.status ? ERROR_STATUS.has(c.status) : false) || c.kind === "intercept",
      };
    });
  }
  const out: Activity[] = [];
  for (const it of orderedItems(run)) {
    if (it.kind === "vote") {
      for (const c of it.casts) {
        out.push({ id: `${it.id}:${c.memberId}`, itemId: it.id, t: ms(it.ts), node: actorNode(c.actor, run), kind: "vote", tokens: 0, cost: 0, error: false });
      }
      continue;
    }
    if (it.kind === "plan" || (it.kind === "system" && it.type === "run.status")) continue;
    const kind = itemKind(it);
    const tokens = it.kind === "thought" ? (it.tokensIn ?? 0) + (it.tokensOut ?? 0) : 0;
    const cost = it.kind === "thought" ? it.costUsd ?? 0 : 0;
    const error = kind === "error" || kind === "intercept" || (it.kind === "tool" && ERROR_STATUS.has(it.status));
    out.push({ id: it.id, itemId: it.id, t: ms(it.ts), node: actorNode(it.actor, run), kind, tokens, cost, error });
    if (it.kind === "thought") {
      for (const ic of it.intercepts) {
        out.push({ id: `${it.id}:${ic.id}`, itemId: it.id, t: ms(it.ts), node: actorNode(ic.actor, run), kind: "intercept", tokens: 0, cost: 0, error: true });
      }
    }
  }
  return out;
}
