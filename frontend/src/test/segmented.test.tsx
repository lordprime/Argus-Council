import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import axe from "axe-core";
import { useState } from "react";
import { describe, expect, it } from "vitest";

import { Segmented, SegmentedTabs } from "@/components/ui";

const OPTS = [
  { value: "a", label: "Alpha" },
  { value: "b", label: "Beta" },
  { value: "c", label: "Gamma" },
] as const;
type V = (typeof OPTS)[number]["value"];

function Radios({ size }: { size?: "sm" | "md" }) {
  const [v, setV] = useState<V>("a");
  return <Segmented label="Pick" value={v} onChange={setV} options={[...OPTS]} size={size} />;
}

function TabsHarness() {
  const [v, setV] = useState<V>("a");
  return (
    <>
      <SegmentedTabs label="Views" value={v} onChange={setV} options={[...OPTS]} idPrefix="t" panelId="t-panel" />
      <div id="t-panel" role="tabpanel" aria-labelledby={`t-tab-${v}`}>Panel {v}</div>
    </>
  );
}

describe("Segmented", () => {
  it("is a radiogroup with one tab stop and arrow-key selection", async () => {
    render(<Radios />);
    const radios = screen.getAllByRole("radio");
    expect(radios.map((r) => r.getAttribute("tabindex"))).toEqual(["0", "-1", "-1"]);
    await userEvent.tab();
    expect(document.activeElement).toBe(radios[0]);
    await userEvent.keyboard("{ArrowRight}");
    expect(screen.getByRole("radio", { name: "Beta" })).toHaveAttribute("aria-checked", "true");
    expect(document.activeElement).toBe(screen.getByRole("radio", { name: "Beta" }));
    await userEvent.keyboard("{End}");
    expect(screen.getByRole("radio", { name: "Gamma" })).toHaveAttribute("aria-checked", "true");
    await userEvent.keyboard("{ArrowRight}"); // wraps
    expect(screen.getByRole("radio", { name: "Alpha" })).toHaveAttribute("aria-checked", "true");
  });

  it("marks the selected segment with a ring and a raised dark-mode surface, not weight alone", () => {
    render(<Radios />);
    const on = screen.getByRole("radio", { name: "Alpha" });
    const off = screen.getByRole("radio", { name: "Beta" });
    expect(on.className).toMatch(/\bring-1\b/);
    expect(on.className).toMatch(/\bring-accent\b/);
    expect(on.className).toMatch(/dark:bg-surface-3/);
    expect(off.className).not.toMatch(/\bring-accent\b/);
  });

  it("supports a small size", () => {
    render(<Radios size="sm" />);
    expect(screen.getByRole("radio", { name: "Alpha" }).className).toMatch(/\bh-7\b/);
  });
});

describe("SegmentedTabs", () => {
  it("wires tabs to the panel with a roving tabindex", async () => {
    const { container } = render(<TabsHarness />);
    const tabs = screen.getAllByRole("tab");
    expect(tabs.map((t) => t.id)).toEqual(["t-tab-a", "t-tab-b", "t-tab-c"]);
    expect(tabs.every((t) => t.getAttribute("aria-controls") === "t-panel")).toBe(true);
    expect(tabs.map((t) => t.getAttribute("tabindex"))).toEqual(["0", "-1", "-1"]);
    tabs[0]!.focus();
    await userEvent.keyboard("{ArrowLeft}");
    expect(screen.getByRole("tab", { name: "Gamma" })).toHaveAttribute("aria-selected", "true");
    expect(document.activeElement).toBe(screen.getByRole("tab", { name: "Gamma" }));
    expect(screen.getByRole("tabpanel", { name: "Gamma" })).toHaveTextContent("Panel c");
    await userEvent.keyboard("{Home}");
    expect(screen.getByRole("tab", { name: "Alpha" })).toHaveAttribute("aria-selected", "true");
    const results = await axe.run(container);
    expect(results.violations.map((v) => v.id)).toEqual([]);
  });
});
