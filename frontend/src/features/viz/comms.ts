// Derives the communication log (CommEvent[]) and sub-council registry from run events.
// Called by the run reducer (src/stores/run.ts) after its own bookkeeping, so the timeline items
// it reads (tool calls, thoughts, approvals) are already up to date for the current event.

import type { Actor, Member, RunEvent } from "@/lib/types";
import type { RunState } from "@/stores/run";

import type { CommEvent, CommKind, NodeRef, SubCouncilInfo } from "./types";

export const COMMS_CAP = 4000;

/** Roles the backend flags as overseers (backend/argus/agents/roles.py). */
export const OVERSEER_ROLES = new Set(["critic", "monitor", "security"]);

export const USER_NODE: NodeRef = { kind: "user", id: "user", label: "You" };
export const COUNCIL_NODE: NodeRef = { kind: "system", id: "council", label: "Council" };

export function isOverseerMember(m: Pick<Member, "config" | "role">): boolean {
  return Boolean(m.config?.overseer ?? OVERSEER_ROLES.has(m.role));
}

export function memberRef(m: Member): NodeRef {
  return { kind: isOverseerMember(m) ? "overseer" : "member", id: m.id, label: m.name, role: m.role };
}

/** Node for whoever sent an event (an agent seat, the user, a remote peer or the system). */
export function actorRef(s: RunState, actor: Actor | undefined): NodeRef {
  if (!actor) return COUNCIL_NODE;
  if (actor.member_id) {
    const m = s.members.find((x) => x.id === actor.member_id);
    if (m) return memberRef(m);
    const overseer = actor.overseer ?? OVERSEER_ROLES.has(actor.role ?? "");
    return { kind: overseer ? "overseer" : "member", id: actor.member_id, label: actor.name ?? actor.role ?? "Agent", role: actor.role };
  }
  if (actor.kind === "user") return USER_NODE;
  if (actor.kind === "remote") return { kind: "remote", id: `remote:${actor.name ?? "peer"}`, label: actor.name ?? "Remote agent" };
  if (actor.kind === "agent" && actor.role) {
    // Agent without a seat id (e.g. a built-in overseer): match by role.
    const m = s.members.find((x) => x.role === actor.role);
    if (m) return memberRef(m);
    const overseer = actor.overseer ?? OVERSEER_ROLES.has(actor.role);
    return { kind: overseer ? "overseer" : "member", id: `role:${actor.role}`, label: actor.name ?? actor.role, role: actor.role };
  }
  return { kind: "system", id: "system", label: "System" };
}

export function memberById(s: RunState, id: string | undefined): NodeRef | undefined {
  if (!id) return undefined;
  const m = s.members.find((x) => x.id === id);
  return m ? memberRef(m) : undefined;
}

export function chairRef(s: RunState): NodeRef {
  const m = s.members.find((x) => x.role === "chair");
  return m ? memberRef(m) : { kind: "system", id: "chair", label: "Chair", role: "chair" };
}

/** Classify a tool name into the node it talks to. */
export function toolTarget(tool: string, args: unknown, parent?: string): NodeRef {
  const ns = parent ? `${parent}:` : "";
  if (tool.startsWith("mcp__")) {
    const server = tool.split("__")[1] || "mcp";
    return { kind: "mcp", id: `${ns}mcp:${server}`, label: server, parent };
  }
  if (tool === "remote_agent") {
    const agent = (args as { agent?: unknown } | null)?.agent;
    const name = typeof agent === "string" && agent ? agent : "remote";
    return { kind: "remote", id: `${ns}remote:${name}`, label: name, parent };
  }
  return { kind: "tool", id: `${ns}tool:${tool}`, label: tool, parent };
}

function protocolTarget(d: Record<string, unknown>, parent?: string): NodeRef {
  const ns = parent ? `${parent}:` : "";
  const detail = (d.detail ?? {}) as Record<string, unknown>;
  if (d.protocol === "mcp") {
    const server = String(detail.server ?? d.peer ?? "mcp");
    return { kind: "mcp", id: `${ns}mcp:${server}`, label: server, parent };
  }
  const agent = String(detail.agent ?? d.peer ?? "remote");
  return { kind: "remote", id: `${ns}remote:${agent}`, label: agent, role: String(d.protocol ?? ""), parent };
}

function subcouncilRef(s: RunState, id: string, parent?: string): NodeRef {
  const info = s.subcouncils[id];
  const label = info?.task ? (info.task.length > 40 ? `${info.task.slice(0, 39)}…` : info.task) : "Sub-council";
  return { kind: "subcouncil", id, label, role: info?.mode, parent };
}

function submemberRef(s: RunState, child: string, actor: Actor | undefined): NodeRef {
  const info = s.subcouncils[child];
  const mid = actor?.member_id ?? (actor?.role ? `role:${actor.role}` : undefined);
  if (!mid) return subcouncilRef(s, child);
  const m = info?.members.find((x) => x.id === actor?.member_id) ?? info?.members.find((x) => !actor?.member_id && x.role === actor?.role);
  return {
    kind: "submember",
    id: `${child}:${m?.id ?? mid}`,
    label: m?.name ?? actor?.name ?? actor?.role ?? "Agent",
    role: m?.role ?? actor?.role,
    parent: child,
  };
}

function short(text: unknown, n = 80): string | undefined {
  if (typeof text !== "string" || !text) return undefined;
  const t = text.replace(/\s+/g, " ").trim();
  return t.length > n ? `${t.slice(0, n - 1)}…` : t;
}

function add(s: RunState, c: CommEvent) {
  s.comms.push(c);
  if (s.comms.length > COMMS_CAP) s.comms.splice(0, s.comms.length - COMMS_CAP);
}

function findComm(s: RunState, id: string): CommEvent | undefined {
  for (let i = s.comms.length - 1; i >= 0; i--) if (s.comms[i]!.id === id) return s.comms[i];
  return undefined;
}

/** Who spawned a sub-council (the member on the parent side of its subcouncil.started comm). */
function spawner(s: RunState, child: string): NodeRef {
  for (let i = s.comms.length - 1; i >= 0; i--) {
    const c = s.comms[i]!;
    if (c.kind === "subcouncil" && c.childRunId === child && c.to.id === child) return c.from;
  }
  return COUNCIL_NODE;
}

/** Find the member who authored the thought an intercept/correction targets. */
function interceptTarget(s: RunState, d: Record<string, any>): NodeRef | undefined { // eslint-disable-line @typescript-eslint/no-explicit-any
  const direct = memberById(s, d.target?.member_id ?? d.member_id);
  if (direct) return direct;
  const tid = d.target?.thought_id;
  if (tid) {
    const t = s.items[tid];
    if (t && "actor" in t) return actorRef(s, t.actor);
  }
  if (d.intercept_id) {
    for (let i = s.order.length - 1; i >= 0; i--) {
      const it = s.items[s.order[i]!];
      if (it?.kind === "thought" && it.intercepts.some((x) => x.id === d.intercept_id)) return actorRef(s, it.actor);
    }
  }
  return undefined;
}

/** Append the CommEvents an event implies and keep the sub-council registry current. */
export function applyComms(s: RunState, ev: RunEvent): void {
  const d = (ev.data ?? {}) as Record<string, any>; // eslint-disable-line @typescript-eslint/no-explicit-any
  const base = { seq: ev.seq, ts: ev.ts, stepId: ev.step_id };
  const mk = (id: string, kind: CommKind, from: NodeRef, to: NodeRef, extra: Partial<CommEvent> = {}): CommEvent => ({
    id, kind, from, to, ...base, ...extra,
  });

  switch (ev.type) {
    case "run.plan": {
      const chair = ev.actor?.member_id ? actorRef(s, ev.actor) : chairRef(s);
      for (const st of (d.steps ?? []) as { id: string; title: string; assignee_member_id?: string; status?: string }[]) {
        const to = memberById(s, st.assignee_member_id);
        if (!to || to.id === chair.id) continue;
        add(s, mk(`${ev.id}:${st.id}`, "assign", chair, to, { label: short(st.title), status: st.status ?? "pending", stepId: st.id }));
      }
      break;
    }
    case "step.status": {
      const step = s.plan?.steps.find((x) => x.id === d.step_id);
      const to = memberById(s, step?.assignee_member_id);
      if (!to) break;
      const chair = chairRef(s);
      if (to.id === chair.id) break;
      add(s, mk(ev.id, "assign", chair, to, { label: short(step?.title), status: d.status, stepId: d.step_id }));
      break;
    }
    case "thought.completed": {
      const from = actorRef(s, ev.actor);
      const chair = chairRef(s);
      const to = from.id === chair.id || chair.kind === "system" ? COUNCIL_NODE : chair;
      add(s, mk(ev.id, "thought", from, to, { label: short(d.text), itemId: d.thought_id }));
      break;
    }
    case "tool.call.started": {
      if (d.tool === "subcouncil") break; // drawn by subcouncil.started
      add(s, mk(ev.id, "tool", actorRef(s, ev.actor), toolTarget(String(d.tool ?? "tool"), d.args), {
        label: d.tool, status: "running", itemId: d.tool_call_id,
      }));
      break;
    }
    case "tool.call.completed": {
      const t = s.items[d.tool_call_id];
      if (t?.kind !== "tool" || t.tool === "subcouncil") break;
      add(s, mk(ev.id, "tool_result", toolTarget(t.tool, t.args), actorRef(s, t.actor), {
        label: t.tool, status: d.status, itemId: d.tool_call_id, stepId: ev.step_id ?? t.stepId,
      }));
      break;
    }
    case "intercept.raised": {
      const from = d.by === "user" ? USER_NODE : actorRef(s, ev.actor);
      add(s, mk(ev.id, "intercept", from, interceptTarget(s, d) ?? COUNCIL_NODE, {
        label: short(d.reason), status: d.action, itemId: d.target?.thought_id ?? d.intercept_id,
      }));
      break;
    }
    case "correction.issued": {
      add(s, mk(ev.id, "correction", actorRef(s, ev.actor), interceptTarget(s, d) ?? COUNCIL_NODE, {
        label: short(d.text), itemId: d.intercept_id,
      }));
      break;
    }
    case "approval.requested":
      add(s, mk(ev.id, "approval", actorRef(s, ev.actor), USER_NODE, {
        label: short(d.action_summary), status: "pending", itemId: d.approval_id,
      }));
      break;
    case "approval.resolved": {
      const a = s.items[d.approval_id];
      const to = a?.kind === "approval" ? actorRef(s, a.actor) : COUNCIL_NODE;
      add(s, mk(ev.id, "approval", USER_NODE, to, { label: short(a?.kind === "approval" ? a.summary : undefined), status: d.decision, itemId: d.approval_id }));
      break;
    }
    case "vote.cast": {
      const from = memberById(s, d.member_id) ?? actorRef(s, ev.actor);
      add(s, mk(ev.id, "vote", from, COUNCIL_NODE, { label: d.option_id, itemId: d.vote_id }));
      break;
    }
    case "user.message":
      add(s, mk(ev.id, "user", USER_NODE, memberById(s, d.member_id ?? d.to_member_id) ?? COUNCIL_NODE, {
        label: short(d.text ?? d.message), itemId: ev.id,
      }));
      break;
    case "protocol.call": {
      const member = actorRef(s, ev.actor);
      const peer = protocolTarget(d);
      const inbound = d.direction === "inbound";
      add(s, mk(ev.id, "protocol", inbound ? peer : member, inbound ? member : peer, {
        label: d.method, status: d.status, itemId: d.tool_call_id,
        ledger: { id: Number(d.ledger_id ?? 0), hash: String(d.hash ?? ""), protocol: String(d.protocol ?? ""), latencyMs: Number(d.latency_ms ?? 0), method: String(d.method ?? "") },
      }));
      break;
    }
    case "subcouncil.started": {
      const id = String(d.child_run_id);
      const info: SubCouncilInfo = {
        id, task: String(d.task ?? ""), mode: String(d.mode ?? "general"), status: "running", depth: Number(d.depth ?? 1),
        members: Array.isArray(d.members) ? d.members : [], startedTs: ev.ts,
      };
      s.subcouncils = { ...s.subcouncils, [id]: info };
      add(s, mk(ev.id, "subcouncil", actorRef(s, ev.actor), subcouncilRef(s, id), {
        label: short(info.task), status: "running", childRunId: id, itemId: d.tool_call_id,
      }));
      break;
    }
    case "subcouncil.completed": {
      const id = String(d.child_run_id);
      const prev = s.subcouncils[id];
      if (prev) s.subcouncils = { ...s.subcouncils, [id]: { ...prev, status: d.status ?? prev.status, endedTs: ev.ts } };
      add(s, mk(ev.id, "subcouncil", subcouncilRef(s, id), actorRef(s, ev.actor), {
        label: short(prev?.task), status: d.status, childRunId: id, itemId: d.tool_call_id,
      }));
      break;
    }
    case "subcouncil.event":
      applyChild(s, ev, d);
      break;
    default:
      break;
  }
}

/** Map one relayed child-run event onto sub-council edges. */
function applyChild(s: RunState, ev: RunEvent, env: Record<string, any>): void { // eslint-disable-line @typescript-eslint/no-explicit-any
  const child = String(env.child_run_id ?? "");
  if (!child) return;
  const cd = (env.data ?? {}) as Record<string, any>; // eslint-disable-line @typescript-eslint/no-explicit-any
  const actor = env.actor as Actor | undefined;
  const hub = subcouncilRef(s, child);
  const from = () => submemberRef(s, child, actor);
  const id = `${ev.id}:${child}:${env.seq ?? ev.seq}`;
  const extra = { seq: ev.seq, ts: ev.ts, childRunId: child };
  const push = (kind: CommKind, f: NodeRef, t: NodeRef, more: Partial<CommEvent> = {}) =>
    add(s, { id, kind, from: f, to: t, ...extra, ...more });
  const subById = (mid: unknown): NodeRef | undefined => {
    const m = s.subcouncils[child]?.members.find((x) => x.id === mid);
    return m ? { kind: "submember", id: `${child}:${m.id}`, label: m.name, role: m.role, parent: child } : undefined;
  };
  const subChair = () => {
    const m = s.subcouncils[child]?.members.find((x) => x.role === "chair");
    return m ? subById(m.id) : undefined;
  };

  switch (env.type) {
    case "run.status": {
      const prev = s.subcouncils[child];
      if (prev && cd.status) {
        const done = ["completed", "failed", "cancelled", "halted"].includes(cd.status);
        s.subcouncils = { ...s.subcouncils, [child]: { ...prev, status: cd.status, endedTs: done ? prev.endedTs ?? ev.ts : prev.endedTs } };
      }
      push("status", hub, spawner(s, child), { status: cd.status, label: cd.status });
      break;
    }
    case "thought.completed": {
      const f = from();
      const chair = subChair();
      push("thought", f, chair && chair.id !== f.id ? chair : hub, { label: short(cd.text) });
      break;
    }
    case "tool.call.started": {
      if (cd.tool === "subcouncil") break;
      add(s, { id: `sub:${child}:tc:${cd.tool_call_id ?? id}`, kind: "tool", from: from(), to: toolTarget(String(cd.tool ?? "tool"), undefined, child), ...extra, label: cd.tool, status: "running" });
      break;
    }
    case "tool.call.completed": {
      const started = findComm(s, `sub:${child}:tc:${cd.tool_call_id}`);
      if (!started) break;
      push("tool_result", started.to, started.from, { label: started.label, status: cd.status });
      break;
    }
    case "intercept.raised":
      push("intercept", from(), subById(cd.member_id) ?? hub, { label: short(cd.reason), status: cd.action });
      break;
    case "correction.issued":
      push("correction", from(), subById(cd.member_id) ?? hub, { label: short(cd.text) });
      break;
    case "approval.requested":
      push("approval", from(), USER_NODE, { status: "pending", label: short(cd.text) });
      break;
    case "vote.cast":
      push("vote", from(), hub);
      break;
    case "step.status": {
      const to = subById(cd.member_id);
      if (to) push("assign", subChair() ?? hub, to, { label: short(cd.title), status: cd.status });
      break;
    }
    case "protocol.call": {
      const f = from();
      const peer = protocolTarget(cd, child);
      push("protocol", f, peer, {
        label: cd.method, status: cd.status,
        ledger: { id: Number(cd.ledger_id ?? 0), hash: String(cd.hash ?? ""), protocol: String(cd.protocol ?? ""), latencyMs: Number(cd.duration_ms ?? cd.latency_ms ?? 0), method: String(cd.method ?? "") },
      });
      break;
    }
    case "subcouncil.started":
      if (cd.child_run_id) push("subcouncil", from(), { kind: "subcouncil", id: String(cd.child_run_id), label: "Sub-council", parent: child }, { status: "running" });
      break;
    case "subcouncil.completed":
      if (cd.child_run_id) push("subcouncil", { kind: "subcouncil", id: String(cd.child_run_id), label: "Sub-council", parent: child }, from(), { status: cd.status });
      break;
    default:
      break;
  }
}
