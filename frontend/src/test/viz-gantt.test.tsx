import { act, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import axe from "axe-core";
import { describe, expect, it, vi } from "vitest";

import RunGantt from "@/components/viz/gantt/RunGantt";
import { buildGantt, fmtDur, tickStep } from "@/components/viz/gantt/model";
import type { Member } from "@/lib/types";
import { emptyRun, type RunState } from "@/stores/run";

const T0 = Date.parse("2026-01-01T00:00:00Z");
const iso = (s: number) => new Date(T0 + s * 1000).toISOString();

const member = (id: string, name: string, role: string, position: number, overseer = false): Member => ({
  id, name, role, provider: "mock", model: "m", system_prompt: "", temperature: null, position, enabled: true,
  config: overseer ? { overseer: true } : {},
});

function synthetic(status = "running"): RunState {
  const run = emptyRun("run_1", [member("m1", "Ada", "researcher", 0), member("m2", "Lin", "writer", 1), member("m3", "Mon", "monitor", 2, true)]);
  run.status = status;
  run.plan = {
    version: 1,
    steps: [
      { id: "s1", title: "Gather sources", assignee_member_id: "m1", status: "completed" },
      { id: "s2", title: "Draft report", assignee_member_id: "m2", status: "running", depends_on: ["s1"] },
    ],
  };
  const add = (it: RunState["items"][string]) => { run.items[it.id] = it; run.order.push(it.id); };
  add({ kind: "thought", id: "th1", seq: 1, ts: iso(0), actor: { kind: "agent", member_id: "m1", role: "researcher", name: "Ada" },
    stepId: "s1", text: "", streaming: false, latencyMs: 4000, tokensIn: 120, tokensOut: 80, costUsd: 0.002, intercepts: [] });
  add({ kind: "tool", id: "tc1", seq: 2, ts: iso(4), actor: { kind: "agent", member_id: "m1", role: "researcher", name: "Ada" },
    stepId: "s1", tool: "web_search", source: "builtin", args: {}, status: "ok", output: "", durationMs: 3000 });
  add({ kind: "thought", id: "th2", seq: 3, ts: iso(8), actor: { kind: "agent", member_id: "m2", role: "writer", name: "Lin" },
    stepId: "s2", text: "", streaming: true, intercepts: [] });
  add({ kind: "approval", id: "ap1", seq: 4, ts: iso(9), actor: { kind: "agent", member_id: "m2", role: "writer", name: "Lin" },
    summary: "write file", risk: [], status: "pending" });
  run.memberStatus = { m1: { status: "idle" }, m2: { status: "thinking" } };
  (run as unknown as { subcouncils: unknown }).subcouncils = [{
    id: "sub1", task: "Verify claims", mode: "debate", status: "running", depth: 1, startedTs: iso(5),
    members: [{ id: "sm1", name: "Checker", role: "critic", overseer: false }],
  }];
  return run;
}

describe("gantt model", () => {
  it("derives intervals from items", () => {
    const m = buildGantt(synthetic("completed"), T0 + 20_000);
    const ada = m.agents.find((a) => a.label === "Ada")!;
    expect(ada.segs.map((s) => [s.kind, s.end - s.start])).toEqual([["think", 4000], ["tool", 3000]]);
    expect(m.steps[0]!.start).toBe(T0);
    expect(m.steps[1]!.deps).toEqual(["step:s1"]);
    expect(m.groups[0]!.members).toHaveLength(1);
    expect(fmtDur(65_000)).toBe("1m 05s");
    expect(tickStep(0.01)).toBe(10_000);
  });
});

describe("RunGantt", () => {
  it("renders step, agent and sub-council rows with a live now line", () => {
    vi.spyOn(Date, "now").mockReturnValue(T0 + 12_000);
    render(<RunGantt run={synthetic()} />);
    const table = screen.getByRole("table", { name: "Run activity timeline" });
    for (const name of ["Gather sources", "Draft report", "Ada", "Lin", "Mon", "Verify claims", "Checker"]) {
      expect(within(table).getByRole("rowheader", { name: new RegExp(name) })).toBeInTheDocument();
    }
    expect(screen.getByTestId("gantt-now")).toHaveTextContent("Now");
    expect(screen.getByRole("button", { name: /Tool · web_search, Ada/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Awaiting approval · write file, Lin, .* to now/ })).toBeInTheDocument();
    vi.restoreAllMocks();
  });

  it("hides the now line once terminal", () => {
    render(<RunGantt run={synthetic("completed")} />);
    expect(screen.queryByTestId("gantt-now")).toBeNull();
  });

  it("calls onSelect on click and Enter, shows a tooltip on focus", async () => {
    const onSelect = vi.fn();
    render(<RunGantt run={synthetic()} onSelect={onSelect} />);
    const bar = screen.getByRole("button", { name: /^Thinking, Ada/ });
    await userEvent.click(bar);
    expect(onSelect).toHaveBeenCalledWith({ itemId: "th1", nodeId: undefined });
    const tool = screen.getByRole("button", { name: /Tool · web_search/ });
    act(() => tool.focus());
    expect(screen.getByRole("tooltip")).toHaveTextContent("web_search");
    expect(tool).toHaveAttribute("aria-describedby", screen.getByRole("tooltip").id);
    await userEvent.keyboard("{Enter}");
    expect(onSelect).toHaveBeenLastCalledWith({ itemId: "tc1", nodeId: undefined });
    fireEvent.keyDown(tool, { key: "ArrowLeft" });
    expect(document.activeElement).toBe(bar);
  });

  it("collapses sub-councils and filters rows", async () => {
    render(<RunGantt run={synthetic()} />);
    await userEvent.click(screen.getByRole("button", { name: "Collapse sub-council Verify claims" }));
    expect(screen.queryByRole("rowheader", { name: /Checker/ })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "steps" }));
    expect(screen.queryByRole("rowheader", { name: /Ada/ })).toBeNull();
    expect(screen.getByRole("rowheader", { name: /Draft report/ })).toBeInTheDocument();
  });

  it("has no axe violations", async () => {
    const { container } = render(<RunGantt run={synthetic()} onSelect={() => {}} />);
    const results = await axe.run(container);
    expect(results.violations.map((v) => v.id)).toEqual([]);
  });
});
