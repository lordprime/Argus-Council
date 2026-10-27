import { describe, expect, it } from "vitest";

import { COMMS_CAP } from "@/features/viz/comms";
import { activityBins, buildGraph, memberTimeline, routeMilestones } from "@/features/viz/model";
import type { Actor, Member, RunEvent } from "@/lib/types";
import { applyEvent, emptyRun, reduceAll, type RunState } from "@/stores/run";

const member = (id: string, role: string, name: string, config: Member["config"] = {}): Member => ({
  id, name, role, provider: "mock", model: "m", system_prompt: "", temperature: null, position: 0, enabled: true, config,
});
const MEMBERS = [
  member("chair", "chair", "Chair"),
  member("coder", "coder", "Coder"),
  member("mon", "monitor", "Monitor"),
  member("sec", "security", "Sec", { overseer: true }),
];
const A = (id: string): Actor => {
  const m = MEMBERS.find((x) => x.id === id)!;
  return { kind: "agent", member_id: id, role: m.role, name: m.name };
};

let seq = 0;
const T0 = Date.parse("2026-01-01T00:00:00Z");
const at = (s: number) => new Date(T0 + s * 1000).toISOString();
const ev = (type: string, data: Record<string, unknown>, t: number, extra: Partial<RunEvent> = {}): RunEvent => {
  const ephemeral = extra.ephemeral ?? false;
  if (!ephemeral) seq += 1;
  return {
    v: 1, seq, id: `e${seq}-${type}${ephemeral ? `-${t}` : ""}`, run_id: "r", ts: at(t), type,
    actor: A("coder"), data, ...extra,
  };
};

function scenario(): RunState {
  seq = 0;
  const events: RunEvent[] = [
    ev("run.status", { status: "running" }, 0, { actor: { kind: "system" } }),
    ev("user.message", { text: "Please build it" }, 0, { actor: { kind: "user" } }),
    ev("run.plan", { version: 1, steps: [
      { id: "s1", title: "Write code", assignee_member_id: "coder", status: "pending" },
      { id: "s2", title: "Review", assignee_member_id: "mon", status: "pending" },
    ] }, 1, { actor: A("chair") }),
    ev("step.status", { step_id: "s1", status: "running" }, 2, { actor: A("chair"), step_id: "s1" }),
    ev("thought.started", { thought_id: "t1" }, 2, { step_id: "s1" }),
    ev("thought.completed", { thought_id: "t1", text: "I will list files", latency_ms: 1000 }, 3, { step_id: "s1" }),
    ev("intercept.raised", { intercept_id: "i1", target: { thought_id: "t1" }, by: "overseer", reason: "drift", severity: "low", action: "annotate" },
      4, { actor: A("mon"), step_id: "s1" }),
    ev("correction.issued", { intercept_id: "i1", text: "Focus" }, 4, { actor: A("mon"), step_id: "s1" }),
    ev("tool.call.started", { tool_call_id: "c1", tool: "shell", source: "builtin", args: {} }, 5, { step_id: "s1" }),
    ev("tool.call.completed", { tool_call_id: "c1", status: "ok", duration_ms: 2000 }, 7, { step_id: "s1" }),
    ev("tool.call.started", { tool_call_id: "c2", tool: "mcp__github__search", source: "mcp", args: {} }, 8, { step_id: "s1" }),
    ev("protocol.call", { ledger_id: 42, hash: "abc", protocol: "mcp", direction: "outbound", peer: "https://gh", method: "tools/call:search",
      status: "ok", latency_ms: 120, tool_call_id: "c2", detail: { server: "github" } }, 8, { step_id: "s1" }),
    ev("tool.call.completed", { tool_call_id: "c2", status: "ok", duration_ms: 500 }, 9, { step_id: "s1" }),
    ev("tool.call.started", { tool_call_id: "c3", tool: "remote_agent", source: "remote", args: { agent: "scout" } }, 10, { step_id: "s1" }),
    ev("protocol.call", { ledger_id: 43, hash: "def", protocol: "a2a", direction: "outbound", peer: "https://scout", method: "message/send",
      status: "ok", latency_ms: 300, tool_call_id: "c3", detail: { agent: "scout" } }, 11, { step_id: "s1" }),
    ev("approval.requested", { approval_id: "a1", action_summary: "rm -rf build", risk: ["shell"] }, 12, { step_id: "s1" }),
    ev("approval.resolved", { approval_id: "a1", decision: "approved" }, 14, { actor: { kind: "user" } }),
    ev("tool.call.started", { tool_call_id: "c4", tool: "subcouncil", source: "builtin", args: { task: "research" } }, 15, { step_id: "s1" }),
    ev("subcouncil.started", { child_run_id: "child", task: "Research libs", mode: "research", depth: 1, tool_call_id: "c4", members: [
      { id: "k1", name: "Kid Chair", role: "chair", overseer: false },
      { id: "k2", name: "Kid Researcher", role: "researcher", overseer: false },
    ] }, 15, { step_id: "s1" }),
    ev("subcouncil.event", { child_run_id: "child", type: "thought.completed", seq: 3, actor: { kind: "agent", member_id: "k2", name: "Kid Researcher", role: "researcher" }, data: { text: "found" } },
      16, { ephemeral: true }),
    ev("subcouncil.event", { child_run_id: "child", type: "tool.call.started", seq: 4, actor: { kind: "agent", member_id: "k2", role: "researcher" }, data: { tool: "web_fetch", tool_call_id: "kc1" } },
      16, { ephemeral: true }),
    ev("subcouncil.event", { child_run_id: "child", type: "tool.call.completed", seq: 5, actor: { kind: "agent", member_id: "k2", role: "researcher" }, data: { tool_call_id: "kc1", status: "ok" } },
      17, { ephemeral: true }),
    ev("subcouncil.event", { child_run_id: "child", type: "run.status", seq: 6, actor: { kind: "system" }, data: { status: "completed" } },
      18, { ephemeral: true }),
    ev("subcouncil.completed", { child_run_id: "child", status: "completed", tool_call_id: "c4" }, 18, { step_id: "s1" }),
    ev("tool.call.completed", { tool_call_id: "c4", status: "ok", duration_ms: 3000 }, 18, { step_id: "s1" }),
    ev("vote.opened", { vote_id: "v1", motion: "Ship?", options: [{ id: "y", label: "Yes" }] }, 19, { actor: A("chair") }),
    ev("vote.cast", { vote_id: "v1", member_id: "mon", option_id: "y" }, 19, { actor: A("mon") }),
  ];
  return reduceAll(emptyRun("r", MEMBERS), events);
}

describe("comms reducer", () => {
  it("derives comm events for every exchange", () => {
    const s = scenario();
    const kinds = s.comms.map((c) => c.kind);
    expect(kinds).toEqual(expect.arrayContaining(["user", "assign", "thought", "intercept", "correction", "tool", "tool_result", "protocol", "approval", "subcouncil", "status", "vote"]));
    // relay events are ephemeral and must not move lastSeq
    expect(s.lastSeq).toBe(seq);

    const assign = s.comms.filter((c) => c.kind === "assign");
    expect(assign[0]).toMatchObject({ from: { id: "chair" }, to: { id: "coder", kind: "member" }, label: "Write code" });
    expect(assign.find((c) => c.status === "running")?.to.id).toBe("coder");

    expect(s.comms.find((c) => c.kind === "thought" && !c.childRunId)).toMatchObject({ from: { id: "coder" }, to: { id: "chair" }, itemId: "t1" });
    expect(s.comms.find((c) => c.kind === "intercept")).toMatchObject({ from: { id: "mon", kind: "overseer" }, to: { id: "coder" } });
    expect(s.comms.find((c) => c.kind === "correction")?.to.id).toBe("coder");

    const tools = s.comms.filter((c) => c.kind === "tool" && !c.childRunId).map((c) => c.to);
    expect(tools).toEqual([
      { kind: "tool", id: "tool:shell", label: "shell" },
      { kind: "mcp", id: "mcp:github", label: "github" },
      { kind: "remote", id: "remote:scout", label: "scout" },
    ]);
    expect(s.comms.find((c) => c.kind === "tool_result" && c.itemId === "c1")).toMatchObject({ from: { id: "tool:shell" }, to: { id: "coder" }, status: "ok" });
    // the subcouncil tool call is drawn as a subcouncil edge, not a tool edge
    expect(s.comms.some((c) => c.label === "subcouncil")).toBe(false);

    const proto = s.comms.filter((c) => c.kind === "protocol");
    expect(proto[0]).toMatchObject({ to: { id: "mcp:github" }, ledger: { id: 42, hash: "abc", protocol: "mcp", latencyMs: 120, method: "tools/call:search" } });
    expect(proto[1]).toMatchObject({ to: { id: "remote:scout", kind: "remote" }, ledger: { id: 43, protocol: "a2a" } });

    const appr = s.comms.filter((c) => c.kind === "approval");
    expect(appr[0]).toMatchObject({ from: { id: "coder" }, to: { id: "user" }, status: "pending" });
    expect(appr[1]).toMatchObject({ from: { id: "user" }, to: { id: "coder" }, status: "approved" });

    expect(s.comms.find((c) => c.kind === "vote")).toMatchObject({ from: { id: "mon" }, to: { id: "council" }, label: "y" });
    expect(s.comms.find((c) => c.kind === "user")).toMatchObject({ from: { id: "user" }, to: { id: "council" } });
    // overseer flag from config takes precedence; role fallback for monitor
    expect(s.comms.find((c) => c.from.id === "mon")?.from.kind).toBe("overseer");
  });

  it("tracks sub-councils and their internal traffic", () => {
    const s = scenario();
    expect(s.subcouncils.child).toMatchObject({ task: "Research libs", mode: "research", depth: 1, status: "completed", startedTs: at(15), endedTs: at(18) });
    expect(s.subcouncils.child?.members).toHaveLength(2);
    const sub = s.comms.filter((c) => c.childRunId === "child");
    expect(sub[0]).toMatchObject({ kind: "subcouncil", from: { id: "coder" }, to: { kind: "subcouncil", id: "child" }, status: "running" });
    const thought = sub.find((c) => c.kind === "thought")!;
    expect(thought.from).toMatchObject({ kind: "submember", id: "child:k2", label: "Kid Researcher", parent: "child" });
    expect(thought.to).toMatchObject({ kind: "submember", id: "child:k1" });
    expect(sub.find((c) => c.kind === "tool")?.to).toMatchObject({ kind: "tool", id: "child:tool:web_fetch", parent: "child" });
    expect(sub.find((c) => c.kind === "tool_result")).toMatchObject({ from: { id: "child:tool:web_fetch" }, to: { id: "child:k2" }, status: "ok" });
    expect(sub.find((c) => c.kind === "status")).toMatchObject({ from: { id: "child" }, to: { id: "coder" }, status: "completed" });
    expect(sub[sub.length - 1]).toMatchObject({ kind: "subcouncil", from: { id: "child" }, to: { id: "coder" }, status: "completed" });
  });

  it("caps the comm log and ignores duplicate events", () => {
    seq = 0;
    const s = emptyRun("r", MEMBERS);
    for (let i = 0; i < COMMS_CAP + 50; i++) applyEvent(s, ev("vote.cast", { vote_id: "v", member_id: "coder", option_id: "y" }, i));
    expect(s.comms).toHaveLength(COMMS_CAP);
    expect(s.comms[s.comms.length - 1]?.seq).toBe(seq);
    const before = s.comms.length;
    applyEvent(s, { ...ev("vote.cast", { vote_id: "v", member_id: "coder", option_id: "y" }, 1), seq: 3 });
    expect(s.comms).toHaveLength(before);
  });

  it("does not mutate the previous state when reducing", () => {
    seq = 0;
    const a = reduceAll(emptyRun("r", MEMBERS), [ev("user.message", { text: "hi" }, 0, { actor: { kind: "user" } })]);
    const b = reduceAll(a, [ev("user.message", { text: "again" }, 1, { actor: { kind: "user" } })]);
    expect(a.comms).toHaveLength(1);
    expect(b.comms).toHaveLength(2);
  });
});

describe("viz selectors", () => {
  it("buildGraph aggregates nodes and edges", () => {
    const s = scenario();
    const g = buildGraph(s);
    const ids = g.nodes.map((n) => n.id);
    expect(ids).toEqual(expect.arrayContaining(["user", "chair", "coder", "mon", "sec", "tool:shell", "mcp:github", "remote:scout", "child", "child:k2", "council"]));
    expect(g.nodes.find((n) => n.id === "sec")).toMatchObject({ kind: "overseer", msgs: 0, active: false });
    expect(g.nodes.find((n) => n.id === "child")).toMatchObject({ kind: "subcouncil", status: "completed" });
    const edge = g.edges.find((e) => e.from === "coder" && e.to === "mcp:github" && e.kind === "protocol");
    expect(edge).toMatchObject({ count: 1, lastTs: at(8) });
    // active: talked within 10 s of the last event (t=19)
    expect(g.nodes.find((n) => n.id === "mon")?.active).toBe(true);
    expect(g.nodes.find((n) => n.id === "tool:shell")?.active).toBe(false);

    const recent = buildGraph(s, { windowMs: 2000 });
    expect(recent.edges.every((e) => Date.parse(e.lastTs) >= T0 + 17_000)).toBe(true);
    expect(recent.nodes.length).toBe(g.nodes.length);
  });

  it("memberTimeline builds spans per node", () => {
    const s = scenario();
    const tl = memberTimeline(s);
    expect(tl.start).toBe(T0);
    expect(tl.end).toBe(T0 + 19_000);
    const coder = tl.rows.find((r) => r.node.id === "coder")!;
    expect(coder.spans.find((x) => x.itemId === "t1")).toMatchObject({ kind: "thinking", start: T0 + 2000, end: T0 + 3000 });
    expect(coder.spans.find((x) => x.itemId === "c1")).toMatchObject({ kind: "tool", start: T0 + 5000, end: T0 + 7000, status: "ok" });
    expect(coder.spans.find((x) => x.itemId === "c3")).toMatchObject({ kind: "tool", end: undefined, status: "running" });
    expect(coder.spans.find((x) => x.itemId === "a1")).toMatchObject({ kind: "waiting", start: T0 + 12_000, end: T0 + 14_000 });
    expect(coder.spans.find((x) => x.kind === "subcouncil")).toMatchObject({ itemId: "child", start: T0 + 15_000, end: T0 + 18_000 });
    expect(coder.spans.some((x) => x.itemId === "c4")).toBe(false);
    expect(tl.rows.find((r) => r.node.id === "child")?.spans).toHaveLength(1);
    // spans sorted
    const starts = coder.spans.map((x) => x.start);
    expect([...starts].sort((a, b) => a - b)).toEqual(starts);
  });

  it("activityBins counts messages per node per bin", () => {
    const s = scenario();
    const h = activityBins(s, 5000);
    expect(h.start).toBe(T0);
    expect(h.bins).toBe(4);
    const total = (id: string) => h.counts[id]!.reduce((a, b) => a + b, 0);
    expect(total("user")).toBe(s.comms.filter((c) => c.from.id === "user" || c.to.id === "user").length);
    expect(h.counts.sec).toEqual([0, 0, 0, 0]);
    expect(h.kinds.coder?.[1]?.tool).toBeGreaterThan(0);
    expect(h.max).toBeGreaterThan(0);
    expect(activityBins(emptyRun("x", MEMBERS), 1000)).toMatchObject({ bins: 0, max: 0 });
  });

  it("routeMilestones pins notable events to plan steps in order", () => {
    const s = scenario();
    const r = routeMilestones(s);
    expect(r.steps.map((x) => [x.id, x.status, x.assignee?.id])).toEqual([["s1", "running", "coder"], ["s2", "pending", "mon"]]);
    const pins = r.steps[0]!.pins.map((p) => p.kind);
    expect(pins).toEqual(["intercept", "correction", "protocol", "protocol", "approval", "subcouncil"]);
    expect(r.steps[0]!.pins.find((p) => p.kind === "approval")?.status).toBe("approved");
    expect(r.steps[0]!.pins.find((p) => p.kind === "subcouncil")).toMatchObject({ status: "completed", childRunId: "child" });
    expect(r.loose.map((p) => p.kind)).toEqual(["vote"]);
    expect(r.all.map((p) => p.seq)).toEqual([...r.all.map((p) => p.seq)].sort((a, b) => a - b));
  });
});
