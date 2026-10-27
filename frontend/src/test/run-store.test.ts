import { beforeEach, describe, expect, it } from "vitest";

import { parseVoteText } from "@/components/run/parts";
import type { RunEvent } from "@/lib/types";
import { emptyRun, ITEMS_CAP, MAX_CACHED_RUNS, reduceAll, useRuns } from "@/stores/run";

let seq = 0;
const ev = (type: string, data: Record<string, unknown>, extra: Partial<RunEvent> = {}): RunEvent => ({
  v: 1, seq: extra.ephemeral ? seq : ++seq, id: `e${seq}-${type}`, run_id: "r", ts: "2026-01-01T00:00:00Z", type,
  actor: { kind: "agent", member_id: "m1", role: "coder", name: "Coder" }, data, ...extra,
});

describe("run reducer", () => {
  it("builds a timeline from the event stream", () => {
    seq = 0;
    const events = [
      ev("run.status", { status: "running" }),
      ev("run.plan", { version: 1, steps: [{ id: "s1", title: "Do it", status: "pending" }] }),
      ev("thought.started", { thought_id: "t1" }),
      ev("thought.delta", { thought_id: "t1", text: "Hel" }, { ephemeral: true }),
      ev("thought.delta", { thought_id: "t1", text: "lo" }, { ephemeral: true }),
    ];
    let s = reduceAll(emptyRun("r"), events);
    expect(s.items.t1).toMatchObject({ kind: "thought", text: "Hello", streaming: true });
    const done = ev("thought.completed", { thought_id: "t1", text: "Hello!", tokens: { in: 3, out: 2 }, cost_usd: 0.01 });
    s = reduceAll(s, [
      done,
      ev("intercept.raised", { intercept_id: "i1", target: { thought_id: "t1" }, by: "overseer", reason: "drift", severity: "medium", action: "hold" },
        { actor: { kind: "agent", role: "monitor", name: "Monitor", overseer: true } }),
      ev("correction.issued", { intercept_id: "i1", text: "Focus" }),
      ev("step.status", { step_id: "s1", status: "done" }),
      ev("tool.call.started", { tool_call_id: "c1", tool: "shell", source: "builtin", args: { command: "ls" } }),
      ev("tool.call.output", { tool_call_id: "c1", stream: "stdout", chunk: "a.txt\n" }, { ephemeral: true }),
      ev("tool.call.completed", { tool_call_id: "c1", status: "ok", result_preview: "a.txt", duration_ms: 12 }),
      ev("approval.requested", { approval_id: "a1", action_summary: "run shell", risk: ["shell"] }),
      ev("budget.update", { used: { usd: 0.5, tokens_in: 1, tokens_out: 2, steps: 3, runtime_s: 4 }, limits: { usd: 5, tokens: null, steps: 80, runtime_s: 3600 } }, { ephemeral: true }),
    ]);
    const t = s.items.t1;
    expect(t).toMatchObject({ streaming: false, text: "Hello!", tokensOut: 2 });
    expect(t?.kind === "thought" && t.intercepts[0]).toMatchObject({ reason: "drift", correction: "Focus" });
    expect(s.plan?.steps[0]?.status).toBe("done");
    expect(s.items.c1).toMatchObject({ status: "ok", output: "a.txt\n", durationMs: 12 });
    expect(s.items.a1).toMatchObject({ kind: "approval", status: "pending" });
    expect(s.budget?.used.usd).toBe(0.5);
    expect(s.status).toBe("running");
  });

  it("ignores duplicate durable events after reconnect", () => {
    seq = 0;
    const a = ev("run.status", { status: "running" });
    const b = ev("user.message", { text: "hi" });
    const s = reduceAll(reduceAll(emptyRun("r"), [a, b]), [a, b]);
    expect(s.order).toHaveLength(2);
    expect(s.lastSeq).toBe(2);
  });

  it("tracks artifacts, exports and sources", () => {
    seq = 0;
    const s = reduceAll(emptyRun("r"), [
      ev("artifact.upserted", { path: "a.py", version: 2, kind: "file", mime: "text/x-python", size: 10 }),
      ev("export.status", { format: "pdf", status: "ready", path: "exports/x.pdf" }),
      ev("citation.added", { source_id: "src_1", n: 1, url: "https://x.io", title: "X", retrieved_at: "now" }),
      ev("citation.added", { source_id: "src_1", n: 1, url: "https://x.io", title: "X", retrieved_at: "now" }),
      ev("artifact.deleted", { path: "a.py" }),
    ]);
    expect(s.artifacts).toEqual({});
    expect(s.exports.pdf?.status).toBe("ready");
    expect(s.sources).toHaveLength(1);
  });
});

describe("run reducer performance contract (implementation review L5)", () => {
  it("copies only the collections a batch writes to and never mutates the previous state", () => {
    seq = 0;
    const prev = reduceAll(emptyRun("r"), [
      ev("run.status", { status: "running" }),
      ev("thought.started", { thought_id: "t1" }),
      ev("user.message", { text: "hi" }, { actor: { kind: "user" } }),
    ]);
    const next = reduceAll(prev, [
      ev("thought.delta", { thought_id: "t1", text: "a" }, { ephemeral: true }),
      ev("thought.delta", { thought_id: "t1", text: "b" }, { ephemeral: true }),
    ]);
    expect(next.items.t1).toMatchObject({ text: "ab" });
    expect(prev.items.t1).toMatchObject({ text: "" });
    expect(next.items).not.toBe(prev.items);
    // Deltas append no item and no comm: those collections keep their identity.
    expect(next.order).toBe(prev.order);
    expect(next.comms).toBe(prev.comms);

    const budget = reduceAll(next, [
      ev("budget.update", { used: { usd: 1, tokens_in: 0, tokens_out: 0, steps: 0, runtime_s: 0 }, limits: { usd: 5, tokens: null, steps: 10, runtime_s: 60 } },
        { ephemeral: true }),
    ]);
    expect(budget.items).toBe(next.items);
    expect(budget.order).toBe(next.order);
  });

  it("attaches intercepts and corrections through the index, also for older thoughts", () => {
    seq = 0;
    const events = [ev("thought.started", { thought_id: "t1" }), ev("thought.completed", { thought_id: "t1", text: "first" })];
    const completed = events[1]!;
    for (let i = 0; i < 50; i++) events.push(ev("user.message", { text: `m${i}` }, { actor: { kind: "user" } }));
    events.push(
      ev("intercept.raised", { intercept_id: "i9", by: "overseer", reason: "drift", severity: "low", action: "annotate" },
        { causation_id: completed.id, actor: { kind: "agent", role: "monitor", name: "Monitor", overseer: true } }),
      ev("correction.issued", { intercept_id: "i9", text: "Refocus" }),
    );
    const s = reduceAll(emptyRun("r"), events);
    const t = s.items.t1;
    expect(t?.kind === "thought" && t.intercepts).toEqual([expect.objectContaining({ id: "i9", correction: "Refocus" })]);
    // The correction attached to the thought, so it is not also a separate timeline row.
    expect(s.order.filter((id) => s.items[id]?.kind === "user")).toHaveLength(50);
  });

  it("caps timeline items, trimming the oldest but keeping pending approvals", () => {
    seq = 0;
    const events = [ev("approval.requested", { approval_id: "a1", action_summary: "run shell", risk: [] })];
    for (let i = 0; i < ITEMS_CAP + 260; i++) events.push(ev("user.message", { text: `m${i}` }, { actor: { kind: "user" } }));
    const s = reduceAll(emptyRun("r"), events);
    expect(s.order.length).toBeLessThanOrEqual(ITEMS_CAP + 250);
    expect(s.order.length).toBeLessThan(events.length);
    expect(s.order[0]).toBe("a1");
    expect(s.items.a1).toMatchObject({ status: "pending" });
    expect(Object.keys(s.items)).toHaveLength(s.order.length);
    expect(s.trimmed).toBe(events.length - s.order.length);
    // The newest event is always kept.
    expect(s.order[s.order.length - 1]).toBe(events[events.length - 1]!.id);
  });
});

describe("runs store cache", () => {
  beforeEach(() => useRuns.setState({ runs: {} }));

  it("keeps only the most recently opened runs and drops their replays", () => {
    const { init } = useRuns.getState();
    init("r1", []);
    useRuns.setState((st) => ({ runs: { ...st.runs, "replay:r1": emptyRun("r1") } }));
    for (let i = 2; i <= MAX_CACHED_RUNS + 1; i++) init(`r${i}`, []);
    const keys = Object.keys(useRuns.getState().runs);
    expect(keys).not.toContain("r1");
    expect(keys).not.toContain("replay:r1");
    expect(keys).toHaveLength(MAX_CACHED_RUNS);

    // Re-opening a cached run marks it recent instead of resetting it.
    seq = 0;
    useRuns.getState().apply("r2", [ev("run.status", { status: "running" })]);
    init("r2", []);
    init("r9", []);
    expect(useRuns.getState().runs.r2?.status).toBe("running");
    expect(useRuns.getState().runs.r3).toBeUndefined();
  });
});

describe("parseVoteText", () => {
  it("reads a ballot written as JSON, fenced or not, and ignores everything else", () => {
    expect(parseVoteText('{"option": "approve", "rationale": "Meets the brief."}')).toEqual({ option: "approve", rationale: "Meets the brief." });
    expect(parseVoteText('```json\n{"option":"reject"}\n```')).toEqual({ option: "reject", rationale: undefined });
    expect(parseVoteText("I vote approve")).toBeNull();
    expect(parseVoteText('{"choice": "approve"}')).toBeNull();
    expect(parseVoteText("{not json}")).toBeNull();
  });
});
