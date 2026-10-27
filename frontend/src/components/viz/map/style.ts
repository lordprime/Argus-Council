// Visual vocabulary for the council map. Literal class strings so Tailwind's JIT sees them all.

import type { CommKind, NodeKind } from "@/features/viz/types";

/** Filter groups shown as chips (tool + tool_result, intercept + correction share a chip). */
export const GROUPS = [
  { id: "assign", label: "Assign", kinds: ["assign"] },
  { id: "thought", label: "Thought", kinds: ["thought"] },
  { id: "tool", label: "Tool", kinds: ["tool", "tool_result"] },
  { id: "intercept", label: "Intercept", kinds: ["intercept", "correction"] },
  { id: "approval", label: "Approval", kinds: ["approval"] },
  { id: "vote", label: "Vote", kinds: ["vote"] },
  { id: "user", label: "User", kinds: ["user"] },
  { id: "protocol", label: "Protocol", kinds: ["protocol"] },
  { id: "subcouncil", label: "Sub-council", kinds: ["subcouncil"] },
  { id: "status", label: "Status", kinds: ["status"] },
] as const satisfies readonly { id: string; label: string; kinds: readonly CommKind[] }[];

export type GroupId = (typeof GROUPS)[number]["id"];

export const GROUP_OF: Record<string, GroupId> = Object.fromEntries(
  GROUPS.flatMap((g) => g.kinds.map((k) => [k, g.id])),
) as Record<string, GroupId>;

/** Conduit stroke per event kind; intercept/correction are also dashed so colour is never the only cue. */
export const EDGE_CLASS: Record<string, string> = {
  assign: "stroke-accent",
  thought: "stroke-accent",
  tool: "stroke-neutral",
  tool_result: "stroke-neutral",
  intercept: "stroke-warning",
  correction: "stroke-danger",
  approval: "stroke-warning",
  vote: "stroke-accent",
  user: "stroke-success",
  protocol: "stroke-info",
  subcouncil: "stroke-accent",
  status: "stroke-neutral",
};

export const EDGE_DASH: Record<string, string | undefined> = {
  intercept: "6 4",
  correction: "2 3",
  approval: "8 3 2 3",
  status: "1 4",
};

export const PULSE_CLASS: Record<string, string> = {
  assign: "fill-accent",
  thought: "fill-accent",
  tool: "fill-neutral",
  tool_result: "fill-neutral",
  intercept: "fill-warning",
  correction: "fill-danger",
  approval: "fill-warning",
  vote: "fill-accent",
  user: "fill-success",
  protocol: "fill-info",
  subcouncil: "fill-accent",
  status: "fill-neutral",
};

export const SWATCH_CLASS: Record<GroupId, string> = {
  assign: "stroke-accent",
  thought: "stroke-accent",
  tool: "stroke-neutral",
  intercept: "stroke-warning",
  approval: "stroke-warning",
  vote: "stroke-accent",
  user: "stroke-success",
  protocol: "stroke-info",
  subcouncil: "stroke-accent",
  status: "stroke-neutral",
};

export const KIND_LABEL: Record<NodeKind, string> = {
  user: "User",
  member: "Member",
  overseer: "Overseer",
  tool: "Tool",
  mcp: "MCP server",
  remote: "Remote agent",
  subcouncil: "Sub-council",
  submember: "Sub-council member",
  system: "Council",
};

/** Status → text fill + short label. Unknown statuses fall back to neutral. */
export const STATUS_TONE: Record<string, { fill: string; label?: string }> = {
  idle: { fill: "fill-fg-subtle" },
  thinking: { fill: "fill-accent" },
  tool: { fill: "fill-info", label: "using tool" },
  waiting: { fill: "fill-warning" },
  held: { fill: "fill-warning" },
  blocked: { fill: "fill-danger" },
  error: { fill: "fill-danger" },
  running: { fill: "fill-info" },
  ok: { fill: "fill-success" },
  completed: { fill: "fill-success" },
  failed: { fill: "fill-danger" },
  pending: { fill: "fill-warning" },
};
