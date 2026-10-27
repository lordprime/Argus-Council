import { describe, expect, it } from "vitest";

import type { RunEvent } from "@/lib/types";
import { emptyRun, reduceAll } from "@/stores/run";

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
