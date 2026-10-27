import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import axe from "axe-core";
import { describe, expect, it, vi } from "vitest";

import CommsHeatmap from "@/components/viz/heatmap/CommsHeatmap";
import { buildHeat, legendRanges, level, pickBin } from "@/components/viz/heatmap/model";
import RunRoute, { fitLabel, LABEL_MIN_PX, PIN_HIT_MIN_PX } from "@/components/viz/route/RunRoute";
import { buildRoute } from "@/components/viz/route/model";
import type { Actor, Member } from "@/lib/types";
import { emptyRun, type RunState } from "@/stores/run";

const T0 = Date.parse("2026-10-27T10:00:00Z");
const at = (s: number) => new Date(T0 + s * 1000).toISOString();

const member = (id: string, name: string, role: string, position: number, overseer = false): Member => ({
  id, name, role, provider: "test", model: "m", system_prompt: "", temperature: null, position, enabled: true, config: { overseer },
});
const members = [member("m1", "Chair", "chair", 0), member("m2", "Coder", "coder", 1), member("m3", "Monitor", "monitor", 2, true)];
const chair: Actor = { kind: "agent", member_id: "m1", role: "chair", name: "Chair" };
const coder: Actor = { kind: "agent", member_id: "m2", role: "coder", name: "Coder" };
const monitor: Actor = { kind: "agent", member_id: "m3", role: "monitor", name: "Monitor", overseer: true };
const peer: Actor = { kind: "remote", name: "peer-a2a", protocol: "a2a" };

function makeRun(status = "running"): RunState {
  const run = emptyRun("run_1", members);
  run.status = status;
  run.plan = {
    version: 1,
    steps: [
      { id: "s1", title: "Research the problem", assignee_member_id: "m1", status: "completed", summary: "Found three options" },
      { id: "s2", title: "Implement the fix", assignee_member_id: "m2", status: "running", depends_on: ["s1"] },
      { id: "s3", title: "Review", assignee_member_id: "m1", status: "pending", depends_on: ["s2"] },
    ],
  };
  const add = (it: RunState["items"][string]) => { run.order.push(it.id); run.items[it.id] = it; };
  add({ kind: "thought", id: "th1", seq: 1, ts: at(0), actor: chair, stepId: "s1", text: "plan", streaming: false, tokensIn: 100, tokensOut: 50, costUsd: 0.01, intercepts: [] });
  add({ kind: "thought", id: "th2", seq: 2, ts: at(40), actor: chair, stepId: "s1", text: "more", streaming: false, tokensIn: 10, tokensOut: 5, intercepts: [] });
  add({ kind: "thought", id: "th3", seq: 3, ts: at(120), actor: coder, stepId: "s2", text: "code", streaming: false, tokensIn: 300, tokensOut: 200, costUsd: 0.05,
    intercepts: [{ id: "ic1", by: "overseer", actor: monitor, reason: "Unsafe shell use", severity: "high", action: "pause", correction: "Use the sandbox", outcome: "applied" }] });
  add({ kind: "tool", id: "tc1", seq: 4, ts: at(150), actor: coder, stepId: "s2", tool: "shell", source: "builtin", args: {}, status: "error", output: "boom" });
  add({ kind: "approval", id: "apr1", seq: 5, ts: at(160), actor: coder, summary: "Coder wants to run shell(ls)", risk: ["shell"], status: "approved" });
  add({ kind: "tool", id: "tc2", seq: 6, ts: at(200), actor: peer, tool: "ask_peer", source: "remote", args: {}, status: "ok", output: "ok" });
  add({ kind: "vote", id: "v1", seq: 7, ts: at(230), motion: "Ship it?", options: [{ id: "y", label: "Yes" }], result: "y",
    casts: [{ memberId: "m1", option: "y", actor: chair }, { memberId: "m2", option: "y", actor: coder }] });
  run.lastSeq = 7;
  return run;
}

async function noAxeViolations(container: HTMLElement) {
  const results = await axe.run(container, { rules: { "color-contrast": { enabled: false } } });
  expect(results.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(", ")}`)).toEqual([]);
}

describe("heatmap model", () => {
  it("picks a bin size that keeps 24–60 columns", () => {
    expect(pickBin(10 * 60 * 1000)).toBe(10_000);
    const h = buildHeat(makeRun());
    expect(h.cols).toBeGreaterThanOrEqual(24);
    expect(h.cols).toBeLessThanOrEqual(60);
    expect(h.rows.map((r) => r.node.label)).toEqual(["Chair", "Coder", "Monitor", "peer-a2a"]);
    const coderRow = h.cells[1]!;
    expect(coderRow.reduce((n, c) => n + c.errors, 0)).toBe(1);
    expect(level(0, 10)).toBe(0);
    expect(level(10, 10)).toBe(5);
    expect(legendRanges(5, "messages")).toEqual(["0", "1", "2", "3", "4", "5"]);
  });

  it("prefers run.comms when present", () => {
    const run = makeRun() as RunState & { comms: unknown[] };
    run.comms = [
      { id: "c1", seq: 1, ts: at(0), kind: "subcouncil", from: { kind: "member", id: "m1", label: "Chair" }, to: { kind: "subcouncil", id: "sc1", label: "Sub" }, childRunId: "run_c" },
      { id: "c2", seq: 2, ts: at(5), kind: "thought", from: { kind: "submember", id: "sc1:a", label: "Sub analyst", role: "analyst", parent: "sc1" }, to: { kind: "member", id: "m1", label: "Chair" } },
      { id: "c3", seq: 3, ts: at(9), kind: "protocol", from: { kind: "member", id: "m2", label: "Coder" }, to: { kind: "remote", id: "peer", label: "Peer" },
        ledger: { id: 1, hash: "abcdef0123456789abcdef", protocol: "a2a", latencyMs: 42, method: "tasks/send" } },
    ];
    const h = buildHeat(run);
    expect(h.rows.find((r) => r.node.id === "sc1:a")?.depth).toBe(1);
    const route = buildRoute(run);
    const proto = route.milestones.find((m) => m.kind === "protocol");
    expect(proto?.hash).toBe("abcdef0123456789abcdef");
    expect(route.milestones.filter((m) => m.kind === "subcouncil")).toHaveLength(1);
  });
});

describe("CommsHeatmap", () => {
  it("renders rows, legend, status and is accessible", async () => {
    const { container } = render(<CommsHeatmap run={makeRun()} />);
    expect(screen.getByRole("heading", { name: "Communication heatmap" })).toBeInTheDocument();
    expect(screen.getByTestId("heatmap-status")).toHaveTextContent("Live");
    expect(screen.getAllByRole("row")).toHaveLength(4);
    expect(within(screen.getByRole("list", { name: "Legend" })).getAllByRole("listitem")).toHaveLength(7);
    expect(container.querySelectorAll('[data-testid="error-hatch"]').length).toBeGreaterThan(0);
    await noAxeViolations(container);
  });

  it("shows ended state", () => {
    render(<CommsHeatmap run={makeRun("completed")} />);
    expect(screen.getByTestId("heatmap-status")).toHaveTextContent("Ended · completed");
  });

  it("shows a tooltip on hover and selects the node on click", async () => {
    const onSelect = vi.fn();
    render(<CommsHeatmap run={makeRun()} onSelect={onSelect} />);
    const cell = screen.getAllByRole("gridcell").find((c) => c.getAttribute("aria-label")?.startsWith("Coder") && c.getAttribute("data-level") !== "0")!;
    fireEvent.mouseEnter(cell);
    const tip = screen.getByRole("tooltip");
    expect(tip).toHaveTextContent("Coder");
    expect(tip).toHaveTextContent(/thought/);
    await userEvent.click(cell);
    expect(onSelect).toHaveBeenCalledWith({ nodeId: "m2" });
  });

  it("supports arrow-key navigation with a roving tabindex and metric toggle", async () => {
    const onSelect = vi.fn();
    render(<CommsHeatmap run={makeRun()} onSelect={onSelect} />);
    const cells = () => screen.getAllByRole("gridcell");
    expect(cells().filter((c) => c.getAttribute("tabindex") === "0")).toHaveLength(1);
    await userEvent.tab(); // the metric control is one radiogroup, so one tab stop
    expect(screen.getByRole("radio", { name: "Messages" })).toHaveFocus();
    await userEvent.tab();
    expect(document.activeElement?.getAttribute("data-cell")).toBe("0-0");
    expect(screen.getByRole("tooltip")).toHaveTextContent("Chair");
    await userEvent.keyboard("{ArrowRight}");
    expect(document.activeElement?.getAttribute("data-cell")).toBe("0-1");
    await userEvent.keyboard("{ArrowDown}");
    expect(document.activeElement?.getAttribute("data-cell")).toBe("1-1");
    expect(cells().filter((c) => c.getAttribute("tabindex") === "0").map((c) => c.getAttribute("data-cell"))).toEqual(["1-1"]);
    await userEvent.keyboard("{End}");
    expect(document.activeElement?.getAttribute("data-cell")).toMatch(/^1-\d+$/);
    await userEvent.keyboard("{Enter}");
    expect(onSelect).toHaveBeenCalledWith({ nodeId: "m2" });
    await userEvent.click(screen.getByRole("radio", { name: "Errors" }));
    expect(screen.getByRole("radio", { name: "Errors" })).toHaveAttribute("aria-checked", "true");
    expect(screen.getByRole("grid")).toHaveAccessibleName(/metric errors/);
  });

  it("draws 11px axis labels with room so the first and last are not clipped", () => {
    const { container } = render(<CommsHeatmap run={makeRun()} />);
    const ticks = Array.from(container.querySelectorAll<SVGTextElement>('[data-testid="heatmap-tick"]'));
    expect(ticks.length).toBeGreaterThan(1);
    const svgW = Number(screen.getByRole("grid").getAttribute("width"));
    const half = (8 * 11 * 0.62) / 2; // widest label, "12:38:30" in the mono face
    for (const t of ticks) {
      expect(t.getAttribute("font-size")).toBe("11");
      expect(Number(t.getAttribute("x")) - half).toBeGreaterThanOrEqual(0);
      expect(Number(t.getAttribute("x")) + half).toBeLessThanOrEqual(svgW);
    }
  });
});

describe("RunRoute", () => {
  it("renders trail, pins and log accessibly", async () => {
    const { container } = render(<RunRoute run={makeRun()} />);
    expect(screen.getByTestId("route-progress")).toHaveTextContent("1 of 3 steps · 33%");
    const log = screen.getByRole("list", { name: "Run log" });
    const entries = within(log).getAllByRole("listitem");
    expect(entries[0]).toHaveTextContent("Start");
    expect(entries.at(-1)).toHaveTextContent("Deliverable");
    for (const k of ["Intercept", "Correction", "Approval", "Vote", "call"]) expect(log).toHaveTextContent(k);
    expect(container.querySelector('[data-testid="trail-done"]')?.getAttribute("d")).toMatch(/^M/);
    expect(container.querySelector('[data-testid="trail-remaining"]')?.getAttribute("stroke-dasharray")).toBe("6 6");
    expect(within(screen.getByRole("group", { name: /^Route map/ })).getByRole("button", { name: /^Alert: Intercept/ })).toBeInTheDocument();
    await noAxeViolations(container);
  });

  it("selects items from pins (keyboard) and from the log", async () => {
    const onSelect = vi.fn();
    render(<RunRoute run={makeRun()} onSelect={onSelect} />);
    const pin = within(screen.getByRole("group", { name: /^Route map/ })).getByRole("button", { name: /^Approval: Approval/ });
    pin.focus();
    await userEvent.keyboard("{Enter}");
    expect(onSelect).toHaveBeenLastCalledWith({ itemId: "apr1", nodeId: undefined });
    expect(pin).toHaveAttribute("aria-pressed", "true");
    const log = screen.getByRole("list", { name: "Run log" });
    await userEvent.click(within(log).getByRole("button", { name: /Vote/ }));
    expect(onSelect).toHaveBeenLastCalledWith({ itemId: "v1", nodeId: undefined });
    expect(within(log).getByRole("button", { name: /Vote/ })).toHaveAttribute("aria-current", "true");
  });

  it("gives every pin a hit circle of at least 12 and labels of at least 11px", () => {
    const { container } = render(<RunRoute run={makeRun()} />);
    const pins = container.querySelectorAll("[data-pin]");
    expect(pins.length).toBeGreaterThan(5);
    for (const pin of pins) {
      const hit = pin.querySelector("[data-hit]");
      expect(hit?.getAttribute("fill")).toBe("transparent");
      expect(Number(hit?.getAttribute("r"))).toBeGreaterThanOrEqual(PIN_HIT_MIN_PX);
    }
    const labels = Array.from(container.querySelectorAll('[data-testid="pin-label"]'));
    expect(labels.length).toBeGreaterThan(0);
    for (const l of labels) expect(Number(l.getAttribute("font-size"))).toBeGreaterThanOrEqual(LABEL_MIN_PX);
    // The viewBox has side margins for labels beside edge pins.
    expect(screen.getByRole("group", { name: /^Route map/ }).getAttribute("viewBox")).toMatch(/^-\d+ 0 /);
  });

  it("truncates labels to the space beside the pin", () => {
    expect(fitLabel("Research the problem", 200, 11)).toBe("Research the problem");
    const cut = fitLabel("Research the problem", 70, 11);
    expect(cut.endsWith("…")).toBe(true);
    expect(cut.length).toBeLessThanOrEqual(Math.floor(70 / (11 * 0.56)));
    expect(fitLabel("Scope the question", 0, 11)).toBe("Sco…");
  });

  it("plants the flag when the run completes", () => {
    const run = makeRun("completed");
    run.plan!.steps = run.plan!.steps.map((s) => ({ ...s, status: "completed" }));
    render(<RunRoute run={run} />);
    expect(screen.getByTestId("route-progress")).toHaveTextContent("3 of 3 steps · 100%");
    expect(within(screen.getByRole("group", { name: /^Route map/ })).getByRole("button", { name: /^Summit: Deliverable, completed/ })).toBeInTheDocument();
  });
});
