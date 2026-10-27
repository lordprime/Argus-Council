import { act, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import axe from "axe-core";
import { afterEach, describe, expect, it, vi } from "vitest";

import RunGantt, { BAR_HIT_MIN, ganttColumns } from "@/components/viz/gantt/RunGantt";
import {
  buildGantt, CHART_GUTTER, effectiveScale, fitScale, FIT_MIN_SPAN, fmtDur, fmtOffset, fmtTick, stepScale, tickStep, ZOOM_MAX, ZOOM_MIN,
} from "@/components/viz/gantt/model";
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

describe("gantt zoom", () => {
  it("fits a run of any length into the pane", () => {
    for (const [dur, pane] of [[1_000, 800], [1_400, 1200], [90_000, 500], [3_600_000, 1400]] as const) {
      const k = fitScale(dur, pane);
      expect(dur * k + CHART_GUTTER).toBeCloseTo(pane, 6);
    }
    // Very short runs stretch over FIT_MIN_SPAN, not a fixed 10s window.
    expect(fitScale(40, 1024)).toBeCloseTo((1024 - CHART_GUTTER) / FIT_MIN_SPAN);
    expect(fitScale(1_200, 1024) * 1_200 + CHART_GUTTER).toBeCloseTo(1024);
    expect(fitScale(Number.NaN, 400)).toBeCloseTo((400 - CHART_GUTTER) / FIT_MIN_SPAN);
  });

  it("follows fit until zoomed by hand and clamps manual zoom relative to fit", () => {
    const fit = fitScale(10_000, 1000);
    expect(effectiveScale(null, fit)).toBe(fit);
    expect(effectiveScale(fit * 2, fit)).toBe(fit * 2);
    expect(effectiveScale(fit * 1000, fit)).toBe(fit * ZOOM_MAX);
    expect(effectiveScale(fit / 1000, fit)).toBe(fit * ZOOM_MIN);
    expect(stepScale(fit, fit, 1)).toBeCloseTo(fit * 1.5);
    expect(stepScale(fit, fit, -1)).toBeCloseTo(fit / 1.5);
    // A manual scale survives a resize: only its clamp moves with the new fit.
    const wider = fitScale(10_000, 2000);
    expect(effectiveScale(fit * 2, wider)).toBe(fit * 2);
  });

  it("labels sub-second ticks and offsets so short runs read", () => {
    expect(tickStep(0.8)).toBe(100);
    expect(tickStep(0.2)).toBe(500);
    expect(fmtTick(250, 250)).toBe("+0.25s");
    expect(fmtTick(500, 100)).toBe("+0.5s");
    expect(fmtTick(65_000, 5_000)).toBe("+1:05");
    expect(fmtTick(10_000, 5_000)).toBe("+10s");
    expect(fmtOffset(420)).toBe("+0.4s");
    expect(fmtOffset(12_300)).toBe("+12s");
    expect(fmtOffset(3_725_000)).toBe("+1:02:05");
  });
});

/** A finished run lasting exactly one second (what the offline mock provider produces). */
function shortRun(): RunState {
  const run = emptyRun("run_s", [member("m1", "Ada", "researcher", 0)]);
  run.status = "completed";
  const add = (it: RunState["items"][string]) => { run.items[it.id] = it; run.order.push(it.id); };
  const ada = { kind: "agent", member_id: "m1", role: "researcher", name: "Ada" } as const;
  add({ kind: "thought", id: "s_th", seq: 1, ts: new Date(T0).toISOString(), actor: ada, text: "", streaming: false, latencyMs: 600, intercepts: [] });
  add({ kind: "tool", id: "s_tc", seq: 2, ts: new Date(T0 + 600).toISOString(), actor: ada, tool: "web_search", source: "builtin", args: {},
    status: "ok", output: "", durationMs: 400 });
  add({ kind: "tool", id: "s_tiny", seq: 3, ts: new Date(T0 + 990).toISOString(), actor: ada, tool: "read_file", source: "builtin", args: {},
    status: "ok", output: "", durationMs: 10 });
  return run;
}

describe("RunGantt zoom and hit areas", () => {
  let width = 1400;
  let observers: (() => void)[] = [];
  const resizeTo = (w: number) => act(() => { width = w; for (const fire of observers) fire(); });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    observers = [];
    width = 1400;
  });

  function setup() {
    vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockImplementation(() => width);
    vi.stubGlobal("ResizeObserver", class {
      constructor(cb: () => void) { observers.push(() => cb()); }
      observe() {}
      unobserve() {}
      disconnect() {}
    });
    render(<RunGantt run={shortRun()} />);
  }
  const chartW = () => parseFloat(screen.getByTestId("gantt-timeline").style.width);
  const barEnd = (name: RegExp) => {
    const btn = screen.getByRole("button", { name });
    const vis = btn.querySelector<HTMLElement>("[data-bar-visual]")!;
    return parseFloat(btn.style.left) + parseFloat(vis.style.left) + parseFloat(vis.style.width);
  };
  const visWidth = (name: RegExp) =>
    parseFloat(screen.getByRole("button", { name }).querySelector<HTMLElement>("[data-bar-visual]")!.style.width);

  it("fits a one-second run to the pane on first render", () => {
    setup();
    const pane = width - ganttColumns(width).tableW;
    expect(chartW()).toBe(pane); // no horizontal scroll
    expect(barEnd(/^Tool · web_search/)).toBeCloseTo(pane - CHART_GUTTER, 0);
    expect(screen.getByTestId("zoom-level")).toHaveTextContent("100%");
    expect(screen.getByRole("button", { name: "Fit run to width" })).toHaveAttribute("aria-pressed", "true");
  });

  it("re-fits on resize until the user zooms, then keeps their scale", async () => {
    setup();
    resizeTo(1000);
    let pane = 1000 - ganttColumns(1000).tableW;
    expect(chartW()).toBe(pane);
    expect(barEnd(/^Tool · web_search/)).toBeCloseTo(pane - CHART_GUTTER, 0);

    await userEvent.click(screen.getByRole("button", { name: "Zoom in" }));
    expect(screen.getByTestId("zoom-level")).toHaveTextContent("150%");
    expect(screen.getByRole("button", { name: "Fit run to width" })).toHaveAttribute("aria-pressed", "false");
    const zoomed = visWidth(/^Tool · web_search/);
    expect(zoomed).toBeCloseTo((400 * (pane - CHART_GUTTER) / 1000) * 1.5, 3);

    resizeTo(1600);
    expect(visWidth(/^Tool · web_search/)).toBeCloseTo(zoomed, 6); // scale kept

    await userEvent.click(screen.getByRole("button", { name: "Fit run to width" }));
    pane = 1600 - ganttColumns(1600).tableW;
    expect(chartW()).toBe(pane);
    expect(screen.getByTestId("zoom-level")).toHaveTextContent("100%");
  });

  it("gives every bar a hit area at least 24px wide, centred on the bar", () => {
    setup();
    const bars = document.querySelectorAll<HTMLElement>("[data-bar]");
    expect(bars.length).toBeGreaterThan(0);
    for (const b of bars) expect(parseFloat(b.style.width)).toBeGreaterThanOrEqual(BAR_HIT_MIN);
    // The 10ms call is drawn a few px wide, but its hit area is 24px and centred on it.
    const tiny = screen.getByRole("button", { name: /^Tool · read_file/ });
    const vis = tiny.querySelector<HTMLElement>("[data-bar-visual]")!;
    const w = parseFloat(vis.style.width);
    expect(w).toBeLessThan(BAR_HIT_MIN);
    expect(parseFloat(tiny.style.width)).toBe(BAR_HIT_MIN);
    expect(parseFloat(vis.style.left)).toBeCloseTo((BAR_HIT_MIN - w) / 2, 6);
  });

  it("stops zooming out at the minimum", () => {
    setup();
    for (let i = 0; i < 6; i++) fireEvent.click(screen.getByRole("button", { name: "Zoom out" }));
    expect(screen.getByRole("button", { name: "Zoom out" })).toBeDisabled();
    expect(screen.getByTestId("zoom-level")).toHaveTextContent(`${ZOOM_MIN * 100}%`);
    for (const b of document.querySelectorAll<HTMLElement>("[data-bar]")) expect(parseFloat(b.style.width)).toBeGreaterThanOrEqual(BAR_HIT_MIN);
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
    await userEvent.click(screen.getByRole("radio", { name: "Steps" }));
    expect(screen.queryByRole("rowheader", { name: /Ada/ })).toBeNull();
    expect(screen.getByRole("rowheader", { name: /Draft report/ })).toBeInTheDocument();
  });

  it("collapses columns by its own width so the bars keep at least 55%", () => {
    for (const w of [360, 590, 899, 900, 1199, 1200, 1600]) {
      const { cols, tableW } = ganttColumns(w);
      expect(tableW / w).toBeLessThanOrEqual(0.45);
      expect(cols.includes("start")).toBe(w >= 1200);
      expect(cols.length === 0).toBe(w < 900);
    }
  });

  it("in a narrow container shows only the name column and still renders the bars", () => {
    const spy = vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(590);
    render(<RunGantt run={synthetic("completed")} />);
    const table = screen.getByRole("table", { name: "Run activity timeline" });
    const headers = within(table).getAllByRole("columnheader").map((h) => h.querySelector(".sr-only")?.textContent ?? h.textContent);
    expect(headers).toEqual(["Name", "Timeline"]);
    expect(within(table).queryByRole("columnheader", { name: /Status|Start|End|Duration|Cost/ })).toBeNull();
    expect(screen.getByTestId("gantt-timeline")).toBeInTheDocument();
    expect(parseFloat(screen.getByTestId("gantt-timeline").style.width)).toBeGreaterThanOrEqual(590 * 0.55);
    expect(screen.getByRole("button", { name: /Tool · web_search, Ada/ })).toBeInTheDocument();
    // Status survives as an icon inside the name cell.
    expect(within(table).getByRole("rowheader", { name: /Gather sources.*completed/ })).toBeInTheDocument();
    spy.mockRestore();
  });

  it("has no axe violations", async () => {
    const { container } = render(<RunGantt run={synthetic()} onSelect={() => {}} />);
    const results = await axe.run(container);
    expect(results.violations.map((v) => v.id)).toEqual([]);
  });
});
