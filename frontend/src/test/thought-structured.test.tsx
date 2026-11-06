// Thought cards: JSON replies as structured views (plans, debate moves, co-signs, a generic tree), with
// "View raw" so nothing is hidden, and each agent's reasoning in a collapsible section.

import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import axe from "axe-core";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it } from "vitest";

import { ThoughtCard } from "@/components/run/parts";
import { classifyJson, extractJson } from "@/features/runs/thoughtJson";
import type { Member, RunEvent } from "@/lib/types";
import { emptyRun, reduceAll, useRuns, type ThoughtItem } from "@/stores/run";
import { useUi } from "@/stores/ui";

let seq = 0;
const ev = (type: string, data: Record<string, unknown>, extra: Partial<RunEvent> = {}): RunEvent => ({
  v: 1, seq: ++seq, id: `e${seq}`, run_id: "r", ts: "2026-11-01T00:00:00Z", type,
  actor: { kind: "agent", member_id: "m1", name: "Ada", role: "chair" }, data, ...extra,
});
const member = (id: string, name: string, role: string, position: number): Member => ({
  id, name, role, provider: "mock", model: "mock-1", system_prompt: "", temperature: null, position, enabled: true, config: {},
});
const MEMBERS = [member("m1", "Ada", "chair", 0), member("m2", "Ben", "analyst", 1), member("m3", "Cy", "writer", 2)];

const thought = (text: string, extra: Partial<ThoughtItem> = {}): ThoughtItem => ({
  kind: "thought", id: "t1", seq: 1, ts: "2026-11-01T00:00:00Z", actor: { kind: "agent", member_id: "m1", name: "Ada", role: "chair" },
  text, streaming: false, intercepts: [], ...extra,
});

function renderCard(t: ThoughtItem) {
  useRuns.setState({ runs: { r: emptyRun("r", MEMBERS) } });
  return render(<MemoryRouter><ThoughtCard runId="r" t={t} live={false} selected={false} onSelect={() => {}} /></MemoryRouter>);
}

async function noAxeViolations(container: HTMLElement) {
  const results = await axe.run(container, { rules: { "color-contrast": { enabled: false } } });
  expect(results.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(", ")}`)).toEqual([]);
}

const LONG = "Survey the 2024 capacity figures from the IEA, IRENA and national grid operators. ".repeat(5).trim();
const PLAN = JSON.stringify({
  steps: [
    { id: "s1", title: "Gather the figures", assignee: "chair", instructions: LONG, depends_on: [] },
    { id: "s2", title: "Check the sources", assignee: "Ben", instructions: "Verify each figure.", depends_on: ["s1"] },
    { id: "s3", title: "Write the brief", assignee: "m3", instructions: "Two pages.", depends_on: ["s1", "s2"] },
  ],
});

afterEach(() => {
  useRuns.setState({ runs: {} });
  useUi.getState().setShowReasoning(true);
});

describe("finding JSON in a reply", () => {
  it("takes the whole text, a fenced block, or an object in prose", () => {
    expect(extractJson('{"a": 1}')?.value).toEqual({ a: 1 });
    const fenced = extractJson('Here it is:\n```json\n{"option": "approve"}\n```\nThanks.');
    expect(fenced).toMatchObject({ before: "Here it is:", after: "Thanks.", raw: '{"option": "approve"}' });
    const inline = extractJson('My review: {"action": "pass", "severity": "low"} and that is all.');
    expect(inline).toMatchObject({ before: "My review:", after: "and that is all.", value: { action: "pass" } });
  });

  it("leaves partial, malformed and non-JSON text alone", () => {
    expect(extractJson('{"steps": [{"id": "s1", "title": "A"}, {"id"')).toBeNull(); // still streaming: no nested match
    expect(extractJson('{"a": 1,}')).toBeNull();
    expect(extractJson("See [Source 1] and {braces} in prose.")).toBeNull();
    expect(extractJson('```python\nx = {"a": 1}\n```')).toBeNull();
    expect(extractJson("{}")).toBeNull();
  });

  it("recognises the shapes agents reply in", () => {
    expect(classifyJson(JSON.parse(PLAN))).toBe("plan");
    expect(classifyJson({ challenges: [{ target: "clm_1", objection: "x", demand: "proof" }], resolutions: [] })).toBe("challenges");
    expect(classifyJson({ responses: [{ challenge_id: "chl_1", stance: "defend" }] })).toBe("responses");
    expect(classifyJson({ conclusion: "x", stances: [] })).toBe("position");
    expect(classifyJson({ action: "amend", reason: "x" })).toBe("cosign");
    expect(classifyJson({ action: "block", reason: "drift", severity: "high" })).toBe("oversight");
    expect(classifyJson({ endorse: ["am_1"] })).toBe("amendments");
    expect(classifyJson({ option: "approve" })).toBe("vote");
    expect(classifyJson({ foo: 1 })).toBe("tree");
  });
});

describe("structured thought views", () => {
  it("shows a plan as a numbered step list with assignees and dependencies, and the raw JSON on request", async () => {
    const { container } = renderCard(thought(PLAN));
    const steps = within(screen.getByRole("list", { name: "Plan steps" })).getAllByRole("listitem");
    expect(steps).toHaveLength(3);
    expect(steps[0]).toHaveTextContent("Gather the figures");
    expect(steps[0]).toHaveTextContent("Assigned to Ada"); // "chair" resolves to the only chair
    expect(steps[1]).toHaveTextContent("Ben");
    expect(steps[1]).toHaveTextContent("after s1");
    expect(steps[2]).toHaveTextContent("Cy"); // by member id
    expect(steps[2]).toHaveTextContent("after s1, s2");
    expect(screen.queryByText(/"depends_on"/)).not.toBeInTheDocument();

    const more = within(steps[0]!).getByRole("button", { name: "Show more" });
    expect(more).toHaveAttribute("aria-expanded", "false");
    await userEvent.click(more);
    expect(within(steps[0]!).getByRole("button", { name: "Show less" })).toHaveAttribute("aria-expanded", "true");

    await noAxeViolations(container);

    const raw = screen.getByRole("button", { name: "View raw" });
    expect(raw).toHaveAttribute("aria-pressed", "false");
    await userEvent.click(raw);
    expect(raw).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("region", { name: "Raw JSON" })).toHaveTextContent('"depends_on":["s1","s2"]');
    expect(screen.queryByRole("list", { name: "Plan steps" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Copy JSON" })).toBeInTheDocument();
    await userEvent.click(raw);
    expect(screen.getByRole("list", { name: "Plan steps" })).toBeInTheDocument();
  });

  it("renders a fenced debate position with the prose around it and stance chips", async () => {
    const position = { conclusion: "Solar leads in 2024.", stances: [{ claim_id: "clm_1", stance: "endorse", reason: "sourced" }, { claim_id: "clm_2", stance: "object", reason: "no source" }], alternative: "" };
    const { container } = renderCard(thought(`Here is my position.\n\n\`\`\`json\n${JSON.stringify(position, null, 2)}\n\`\`\`\n\nHappy to revise.`));
    expect(screen.getByText("Here is my position.")).toBeInTheDocument();
    expect(screen.getByText("Happy to revise.")).toBeInTheDocument();
    const block = screen.getByRole("group", { name: "Position" });
    expect(within(block).getByText("Solar leads in 2024.")).toBeInTheDocument();
    const stances = within(within(block).getByRole("list", { name: "Stances" })).getAllByRole("listitem");
    expect(stances[0]).toHaveTextContent("clm_1endorsesourced");
    expect(stances[1]).toHaveTextContent("clm_2objectno source");
    expect(screen.queryByText(/```/)).not.toBeInTheDocument();
    await noAxeViolations(container);
  });

  it("renders a co-sign and a challenge round as labelled rows", async () => {
    const cosign = { action: "amend", reason: "Overstates wind", amendment: { passage: "Wind will dominate", replacement: "Wind may grow" } };
    const { container, unmount } = renderCard(thought(JSON.stringify(cosign)));
    const block = screen.getByRole("group", { name: "Co-sign" });
    expect(within(block).getByText("amend")).toBeInTheDocument();
    expect(within(block).getByText("Overstates wind")).toBeInTheDocument();
    expect(within(block).getByText("Replace")).toBeInTheDocument();
    expect(within(block).getByText("Wind may grow")).toBeInTheDocument();
    await noAxeViolations(container);
    unmount();

    const round = {
      challenges: [{ target: "Ben", objection: "That figure is from 2023", demand: "proof" }],
      resolutions: [{ challenge_id: "chl_1", outcome: "accepted", note: "Table 3 confirms it" }],
      claims: [{ statement: "Solar grew 41 GW", evidence: [{ source: "https://iea.org/t3", quote: "41 GW" }], confidence: 0.8 }],
    };
    renderCard(thought(JSON.stringify(round)));
    const challenges = screen.getByRole("list", { name: "Challenges" });
    expect(challenges).toHaveTextContent("Challenges Ben");
    expect(within(challenges).getByText("demands proof")).toBeInTheDocument();
    expect(screen.getByText("accepted")).toBeInTheDocument();
    expect(screen.getByText("80% confident")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "https://iea.org/t3" })).toHaveAttribute("rel", "noopener noreferrer nofollow");
  });

  it("shows unknown JSON as a collapsible key/value tree", async () => {
    const { container } = renderCard(thought(JSON.stringify({ summary: "ok", details: { region: "EU", nested: { deep: true } }, tags: ["a", "b"] })));
    const block = screen.getByRole("group", { name: "JSON" });
    expect(within(block).getByText("summary")).toBeInTheDocument();
    expect(within(block).getByText("ok")).toBeInTheDocument();
    expect(within(block).getByText("details")).toBeInTheDocument();
    expect(within(block).getByText("EU")).toBeInTheDocument();
    expect(within(block).getByText("2 items")).toBeInTheDocument();
    // Depth 0 is open, deeper levels start collapsed.
    const nested = within(block).getByText("nested").closest("details")!;
    expect(nested).not.toHaveAttribute("open");
    await noAxeViolations(container);
  });

  it("falls back to text for malformed or still-streaming JSON", () => {
    renderCard(thought('{"steps": [{"id": "s1", "title": "Gather"}, {"id": "s2"', { streaming: true }));
    expect(screen.queryByRole("group")).not.toBeInTheDocument();
    expect(screen.getByText(/"title": "Gather"/)).toBeInTheDocument();
  });
});

describe("reasoning", () => {
  it("accumulates reasoning deltas and takes the completed reasoning as final", () => {
    seq = 0;
    const started = reduceAll(emptyRun("r", MEMBERS), [
      ev("thought.started", { thought_id: "t1", kind: "message" }),
      ev("thought.reasoning.delta", { thought_id: "t1", text: "First, " }, { ephemeral: true }),
      ev("thought.reasoning.delta", { thought_id: "t1", text: "the figures." }, { ephemeral: true }),
    ]);
    const live = started.items.t1 as ThoughtItem;
    expect(live.reasoning).toBe("First, the figures.");
    expect(live.streaming).toBe(true);
    const done = reduceAll(started, [
      ev("thought.completed", { thought_id: "t1", text: "Solar leads.", reasoning: "First, the 2024 figures.", reasoning_tokens: 1200, tokens: { in: 10, out: 5 } }),
    ]).items.t1 as ThoughtItem;
    expect(done.reasoning).toBe("First, the 2024 figures.");
    expect(done.reasoningTokens).toBe(1200);
    // A model with no reasoning completes with "".
    const plain = reduceAll(emptyRun("r", MEMBERS), [
      ev("thought.started", { thought_id: "t2" }),
      ev("thought.completed", { thought_id: "t2", text: "Hi", reasoning: "" }),
    ]).items.t2 as ThoughtItem;
    expect(plain.reasoning).toBe("");
  });

  it("is open and live while only reasoning has arrived", async () => {
    const { container } = renderCard(thought("", { streaming: true, reasoning: "Weighing the IEA table" }));
    const toggle = screen.getByRole("button", { name: /^Reasoning of Ada/ });
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("region", { name: "Reasoning of Ada" })).toHaveTextContent("Weighing the IEA table");
    await noAxeViolations(container);
  });

  it("is collapsed once the answer exists, opens on request, and shows its token count", async () => {
    const { container } = renderCard(thought("Solar leads.", { reasoning: "Weighing the IEA table", reasoningTokens: 1200 }));
    const toggle = screen.getByRole("button", { name: /^Reasoning of Ada/ });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(toggle).toHaveTextContent("1.2K tok");
    expect(screen.queryByRole("region", { name: "Reasoning of Ada" })).not.toBeInTheDocument();
    expect(screen.getByText("Solar leads.")).toBeInTheDocument();
    await noAxeViolations(container);
    await userEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("region", { name: "Reasoning of Ada" })).toHaveTextContent("Weighing the IEA table");
    await noAxeViolations(container);
  });

  it("is hidden when the run's reasoning toggle is off, and absent when the model gave none", () => {
    act(() => useUi.getState().setShowReasoning(false));
    const { unmount } = renderCard(thought("Solar leads.", { reasoning: "Weighing" }));
    expect(screen.queryByRole("button", { name: /^Reasoning/ })).not.toBeInTheDocument();
    unmount();
    act(() => useUi.getState().setShowReasoning(true));
    renderCard(thought("Solar leads.", { reasoning: "" }));
    expect(screen.queryByRole("button", { name: /^Reasoning/ })).not.toBeInTheDocument();
  });
});
