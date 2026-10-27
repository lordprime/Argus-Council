import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import axe from "axe-core";
import { describe, expect, it, vi } from "vitest";

import { RunStateBadge, VizPane, VizToolbar, ZoomControls } from "@/components/viz/VizToolbar";

describe("ZoomControls", () => {
  it("renders − / % / + / reset / fit in that order and wires each action", async () => {
    const h = { out: vi.fn(), in: vi.fn(), reset: vi.fn(), fit: vi.fn() };
    render(<ZoomControls value={1.5} min={0.5} max={4} onZoomOut={h.out} onZoomIn={h.in} onReset={h.reset} onFit={h.fit} fitted={false} />);
    const group = screen.getByRole("group", { name: "Zoom" });
    expect(Array.from(group.children).map((el) => el.getAttribute("aria-label")))
      .toEqual(["Zoom out", "Zoom level", "Zoom in", "Reset zoom", "Fit to view"]);
    expect(within(group).getByTestId("zoom-level")).toHaveTextContent("150%");
    for (const [name, fn] of [["Zoom out", h.out], ["Zoom in", h.in], ["Reset zoom", h.reset], ["Fit to view", h.fit]] as const) {
      await userEvent.click(within(group).getByRole("button", { name }));
      expect(fn).toHaveBeenCalledTimes(1);
    }
  });

  it("disables the ends of the range and omits optional buttons", () => {
    const { rerender } = render(<ZoomControls value={0.5} min={0.5} max={4} onZoomOut={() => {}} onZoomIn={() => {}} />);
    expect(screen.getByRole("button", { name: "Zoom out" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Zoom in" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: /Reset|Fit/ })).toBeNull();
    rerender(<ZoomControls value={4} min={0.5} max={4} onZoomOut={() => {}} onZoomIn={() => {}} />);
    expect(screen.getByRole("button", { name: "Zoom in" })).toBeDisabled();
  });
});

describe("RunStateBadge", () => {
  it("says Live while running and Ended with the status otherwise", () => {
    const { rerender } = render(<RunStateBadge status="running" testId="s" />);
    expect(screen.getByTestId("s")).toHaveTextContent("Live");
    rerender(<RunStateBadge status="failed" testId="s" />);
    expect(screen.getByTestId("s")).toHaveTextContent("Ended · failed");
    expect(screen.getByTestId("s").querySelector("svg")).not.toBeNull(); // icon, not colour alone
  });
});

describe("VizToolbar", () => {
  it("labels the pane with its title and is accessible", async () => {
    const { container } = render(
      <VizPane labelledBy="t">
        <VizToolbar titleId="t" title="Gantt chart" meta={<RunStateBadge status="completed" />}
          zoom={<ZoomControls value={1} min={0.5} max={4} onZoomOut={() => {}} onZoomIn={() => {}} onFit={() => {}} fitted />}>
          <button type="button">Filter</button>
        </VizToolbar>
      </VizPane>,
    );
    expect(screen.getByRole("region", { name: "Gantt chart" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 2, name: "Gantt chart" })).toBeInTheDocument();
    const results = await axe.run(container);
    expect(results.violations.map((v) => v.id)).toEqual([]);
  });
});
