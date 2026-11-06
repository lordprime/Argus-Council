// A2A messages between council members (and to the user): the run store, the timeline row, and the
// edges they draw in the communication views.

import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";

import { SimpleRow } from "@/components/run/parts";
import type { RunEvent } from "@/lib/types";
import { emptyRun, reduceAll, type SimpleItem } from "@/stores/run";

let seq = 0;
const ev = (type: string, data: Record<string, unknown>, actor: RunEvent["actor"] = { kind: "system", name: "Argus" }): RunEvent => ({
  v: 1, seq: ++seq, id: `e${seq}`, run_id: "r", ts: "2026-11-01T00:00:00Z", type, actor, data,
});
const ada = { member_id: "m1", name: "Ada", role: "researcher" };
const ben = { member_id: "m2", name: "Ben", role: "analyst" };
const message = (role: string, text: string, metadata: Record<string, unknown>) => ({
  message: { kind: "message", messageId: `msg_${seq + 1}`, contextId: "r", role, parts: [{ kind: "text", text }], metadata },
  method: "message/send", status: "ok", direction: "internal", ledger_id: 7, hash: "abc",
});

function run() {
  seq = 0;
  const members = [
    { id: "m1", name: "Ada", role: "researcher", provider: "p", model: "m", position: 0, enabled: true, config: {} },
    { id: "m2", name: "Ben", role: "analyst", provider: "p", model: "m", position: 1, enabled: true, config: {} },
  ];
  return reduceAll({ ...emptyRun("r"), members } as never, [
    ev("a2a.message", message("agent", "What was 2024 capacity?", { from: ada, to: [ben], form: "request" }), { kind: "agent", member_id: "m1", name: "Ada" }),
    ev("a2a.message", message("agent", "41 GW, IEA table 3", { from: ben, to: [ada], form: "request", inReplyTo: "msg_1" }), { kind: "agent", member_id: "m2", name: "Ben" }),
    ev("a2a.message", message("user", "Use 2025+ sources", { from: { kind: "user", name: "You" }, to: [{ name: "council", kind: "council" }], form: "broadcast" }), { kind: "user", name: "You" }),
    ev("a2a.message", message("agent", "Switched to 2025 sources", { from: ben, to: [{ kind: "user", name: "You" }], form: "direct" }), { kind: "agent", member_id: "m2", name: "Ben" }),
    ev("a2a.task", { task_id: "task_1", state: "working" }),
  ]);
}

describe("A2A messages in a run", () => {
  it("shows exchanges between members and replies to the user, but not the user's own message twice", () => {
    const s = run();
    expect(s.order).toEqual(["e1", "e2", "e4"]);
    render(
      <MemoryRouter>
        {s.order.map((id) => <SimpleRow key={id} item={s.items[id] as SimpleItem} onOpenArtifact={() => {}} />)}
      </MemoryRouter>,
    );
    expect(screen.getByText("Question")).toBeInTheDocument();
    expect(screen.getByText("Answer")).toBeInTheDocument();
    expect(screen.getByText(/What was 2024 capacity\?/)).toBeInTheDocument();
    expect(screen.getByText("Ben replied to you")).toBeInTheDocument();
    expect(screen.getByText("Switched to 2025 sources")).toBeInTheDocument();
  });

  it("draws member-to-member and user edges in the communication views", () => {
    const s = run() as unknown as { comms: { kind: string; from: { id: string }; to: { id: string }; label?: string; ledger?: { protocol: string } }[] };
    const a2a = s.comms.filter((c) => c.kind === "a2a");
    expect(a2a.map((c) => `${c.from.id}->${c.to.id}:${c.label}`)).toEqual([
      "m1->m2:request", "m2->m1:request", "user->council:broadcast", "m2->user:direct",
    ]);
    expect(a2a[0]!.ledger?.protocol).toBe("a2a");
  });
});
