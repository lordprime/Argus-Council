// Run workspace on a small screen (consistency audit C-03, C-04, C-08, C-16, C-25, C-26). jsdom's
// matchMedia matches nothing, so the inspector and council rail render as drawers and the intervention
// bar takes its single-row form.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import axe from "axe-core";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { InterventionBar } from "@/components/run/parts";
import type { Member, RunDetail } from "@/lib/types";
import RunWorkspace from "@/routes/RunWorkspace";
import { emptyRun, useRuns, type ThoughtItem } from "@/stores/run";

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

const MEMBERS: Member[] = [
  { id: "m1", name: "Ana", role: "analyst", provider: "mock", model: "mock-1", system_prompt: "", temperature: 0.3, position: 0, enabled: true, config: {} },
];
const ANA = { kind: "agent" as const, member_id: "m1", role: "analyst", name: "Ana" };

const RUN = {
  id: "r1", council_id: null, parent_run_id: null, topic: "Compare open-source vector databases", description: "For a 10M-row corpus",
  mode: "research", status: "completed", outputs: ["md"], guardrails: { budget: { usd: 5, tokens: null } }, usage: { usd: 1.2, steps: 3, runtime_s: 42 },
  error: null, last_seq: 3, created_at: "2026-10-27T10:00:00Z", started_at: "2026-10-27T10:00:00Z", finished_at: "2026-10-27T10:01:00Z",
  result: "# Report\n\nDone.", council_snapshot: { council_id: null, name: "Auto council", members: MEMBERS },
} as unknown as RunDetail;

const thought = (id: string, seq: number, text: string): ThoughtItem => ({
  kind: "thought", id, seq, ts: "2026-10-27T10:00:10Z", actor: ANA, text, streaming: false, tokensIn: 10, tokensOut: 5, costUsd: 0.01, intercepts: [],
});

// The stream replays a terminal status the store already has (seq 3), so it closes without reconnecting.
const STREAM = `data: ${JSON.stringify({
  v: 1, seq: 3, id: "e3", run_id: "r1", ts: "2026-10-27T10:01:00Z", type: "run.status", actor: { kind: "system" }, data: { status: "completed" },
})}\n\n`;

function mockApi() {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const path = String(input).split("?")[0];
    if (path === "/api/v1/runs/r1") return json(RUN);
    if (path === "/api/v1/runs/r1/artifacts") return json({ artifacts: [] });
    if (path === "/api/v1/runs/r1/events/stream") return new Response(STREAM, { status: 200, headers: { "content-type": "text/event-stream" } });
    return json({ error: { code: "not_found", message: path } }, 404);
  });
}

function renderWorkspace(path = "/runs/r1") {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/runs/:runId/*" element={<RunWorkspace />} />
          <Route path="/" element={<h1>Home composer</h1>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

async function noAxeViolations(container: HTMLElement) {
  const results = await axe.run(container, { rules: { "color-contrast": { enabled: false } } });
  expect(results.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(", ")}`)).toEqual([]);
}

beforeEach(() => {
  try {
    localStorage.removeItem("argus.runView");
  } catch {
    /* storage unavailable */
  }
  useRuns.setState({
    runs: {
      r1: {
        ...emptyRun("r1", MEMBERS),
        status: "completed",
        lastSeq: 3,
        order: ["t1", "t2"],
        items: {
          t1: thought("t1", 1, "# Findings\n\nAll three handle the load."),
          t2: thought("t2", 2, '{"option": "approve", "rationale": "Meets the brief."}'),
        },
        budget: {
          used: { usd: 1.2, tokens_in: 900, tokens_out: 300, steps: 3, runtime_s: 42 },
          limits: { usd: 5, tokens: null, steps: 80, runtime_s: 3600 },
        },
      },
    },
  });
});
afterEach(() => vi.restoreAllMocks());

describe("RunWorkspace", () => {
  it("has one h1, labelled meters, ballots in words and a finished-run row", async () => {
    mockApi();
    const { container } = renderWorkspace();
    expect(await screen.findByRole("heading", { level: 1, name: RUN.topic })).toBeInTheDocument();
    // Agent Markdown is demoted under the page h1 (C-08).
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    expect(screen.getByRole("heading", { level: 2, name: "Findings" })).toBeInTheDocument();

    // Budget meters: visible labels and a value text (C-25).
    expect(screen.getByText("Budget")).toBeInTheDocument();
    expect(screen.getByText("Tokens")).toBeInTheDocument();
    expect(screen.getByRole("meter", { name: "Dollar budget" })).toHaveAttribute("aria-valuetext", expect.stringContaining("24 percent of dollar budget"));
    expect(screen.getByRole("meter", { name: "Step budget" })).toHaveAttribute("aria-valuenow", "4");

    // A ballot message reads as a vote, not JSON (C-26).
    expect(screen.getByText(/^Voted/)).toHaveTextContent("Voted approve · Meets the brief.");
    expect(screen.queryByText(/"option"/)).not.toBeInTheDocument();

    // Page switch named apart from the view tabs (C-16).
    const pages = screen.getByRole("navigation", { name: "Run pages" });
    expect(within(pages).getByRole("link", { name: "Workspace" })).toHaveAttribute("aria-current", "page");
    expect(within(pages).getByRole("link", { name: "Deliverable" })).toHaveAttribute("href", "/runs/r1/output");

    // Finished: one line with a follow-up action instead of disabled controls (C-16).
    expect(screen.queryByRole("form", { name: "Intervene" })).not.toBeInTheDocument();
    expect(screen.getByText("Run completed")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Start a follow-up run" })).toHaveAttribute("href", "/");

    await noAxeViolations(container);
  });

  it("opens the inspector in a drawer when an item is inspected below xl", async () => {
    mockApi();
    renderWorkspace();
    await screen.findByRole("heading", { level: 1, name: RUN.topic });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

    await userEvent.click(screen.getAllByRole("button", { name: "Inspect" })[0]!);
    const drawer = await screen.findByRole("dialog", { name: "Inspector" });
    expect(within(drawer).getByRole("tab", { name: "Detail" })).toHaveAttribute("aria-selected", "true");
    expect(within(drawer).getByText("Ana")).toBeInTheDocument();
    await noAxeViolations(drawer);

    await userEvent.click(within(drawer).getByRole("button", { name: "Close inspector" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Inspector" })).not.toBeInTheDocument());

    // The toolbar button opens it too.
    await userEvent.click(screen.getByRole("button", { name: "Inspector" }));
    expect(await screen.findByRole("dialog", { name: "Inspector" })).toBeInTheDocument();
  });

  it("opens the council rail in a sheet below md, and focusing a seat closes it", async () => {
    mockApi();
    renderWorkspace();
    await screen.findByRole("heading", { level: 1, name: RUN.topic });
    await userEvent.click(screen.getByRole("button", { name: "Council" }));
    const sheet = await screen.findByRole("dialog", { name: "Council" });
    expect(within(sheet).getByText("Ana")).toBeInTheDocument();
    await userEvent.click(within(sheet).getByRole("button", { name: "Focus" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Council" })).not.toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Clear focus" })).toBeInTheDocument();
  });

  it("prefills the composer for a follow-up run", async () => {
    mockApi();
    renderWorkspace();
    await userEvent.click(await screen.findByRole("link", { name: "Start a follow-up run" }));
    expect(await screen.findByRole("heading", { name: "Home composer" })).toBeInTheDocument();
  });
});

describe("InterventionBar on a phone", () => {
  it("is a single row whose recipient and type live in a popover", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(json({ ok: true }, 202));
    const { container } = render(
      <MemoryRouter><InterventionBar runId="r1" members={MEMBERS} disabled={false} /></MemoryRouter>,
    );
    const form = screen.getByRole("form", { name: "Intervene" });
    // Selects are not inline; the trigger names the current recipient first.
    expect(within(form).queryByRole("combobox")).not.toBeInTheDocument();
    const trigger = within(form).getByRole("button", { name: /^Council, message/ });
    await noAxeViolations(container);

    await userEvent.click(trigger);
    await userEvent.selectOptions(await screen.findByLabelText("Send to"), "m1");
    expect(within(form).getByRole("button", { name: /^Ana, message/ })).toBeInTheDocument();

    const send = within(form).getByRole("button", { name: "Send" });
    expect(send).toBeDisabled();
    await userEvent.type(within(form).getByLabelText("Message"), "Cite your sources");
    await userEvent.click(send);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/v1/runs/r1/interventions", expect.objectContaining({ method: "POST" })));
    const body = JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body));
    expect(body).toEqual({ kind: "message", text: "Cite your sources", target: { member_id: "m1" } });
  });
});
