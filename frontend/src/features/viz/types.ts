// Shared contract for the live run visualisations (Map, Gantt, Heatmap, Route).
// Populated by the run reducer (src/stores/run.ts) from the SSE stream + protocol ledger.

export type NodeKind =
  | "user"
  | "member"
  | "overseer"
  | "tool"
  | "mcp"
  | "remote"
  | "subcouncil"
  | "submember"
  | "system";

export interface NodeRef {
  kind: NodeKind;
  id: string;
  label: string;
  role?: string;
  /** subcouncil id (child run id) for submember / sub-tool nodes */
  parent?: string;
}

export type CommKind =
  | "assign"
  | "thought"
  | "tool"
  | "tool_result"
  | "intercept"
  | "correction"
  | "approval"
  | "vote"
  | "user"
  | "protocol"
  | "subcouncil"
  | "status";

export interface CommEvent {
  id: string;
  seq: number;
  ts: string;
  kind: CommKind;
  from: NodeRef;
  to: NodeRef;
  label?: string;
  status?: string;
  childRunId?: string;
  ledger?: { id: number; hash: string; protocol: string; latencyMs: number; method: string };
  /** Timeline item this exchange belongs to (thought/tool call/approval/vote id), for onSelect. */
  itemId?: string;
  /** Plan step the event was emitted under, when known. */
  stepId?: string;
}

export interface SubCouncilInfo {
  id: string;
  task: string;
  mode: string;
  status: string;
  depth: number;
  members: { id: string; name: string; role: string; overseer: boolean }[];
  startedTs: string;
  endedTs?: string;
}

export interface CommGraph {
  nodes: (NodeRef & { active: boolean; status?: string; msgs: number })[];
  edges: { id: string; from: string; to: string; kind: CommKind; count: number; lastTs: string }[];
}
