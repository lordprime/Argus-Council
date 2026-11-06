// Resuming a stopped run and changing a live run's budget: the controls, the endpoints they call,
// and how the run store and timeline show what happened.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import { GuardrailsEditor } from "@/components/councils/GuardrailsEditor";
import { BudgetControl } from "@/components/run/BudgetControl";
import { SimpleRow } from "@/components/run/parts";
import { ResumeRunButton } from "@/components/run/ResumeRunButton";
import type { RunEvent } from "@/lib/types";
import { emptyRun, reduceAll, useRuns, type SimpleItem } from "@/stores/run";

let seq = 0;
const ev = (type: string, data: Record<string, unknown>, actor: RunEvent["actor"] = { kind: "system", name: "Argus" }): RunEvent => ({
  v: 1, seq: ++seq, id: `e${seq}`, run_id: "r", ts: "2026-11-01T00:00:00Z", type, actor, data,
});
const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

function wrap(ui: ReactNode, path = "/runs/old") {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/runs/old" element={ui} />
          <Route path="/runs/:id" element={<h1>Opened the resumed run</h1>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.restoreAllMocks();
  useRuns.setState({ runs: {} });
});

describe("resuming a stopped run", () => {
  it("posts to /continue and opens the new run", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(json({ id: "new", resumed_from_id: "old" }, 201));
    wrap(<ResumeRunButton runId="old" />);
    await userEvent.click(screen.getByRole("button", { name: "Resume where it stopped" }));
    expect(await screen.findByRole("heading", { name: "Opened the resumed run" })).toBeInTheDocument();
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(String(url)).toBe("/api/v1/runs/old/continue");
    expect(init?.method).toBe("POST");
  });

  it("keeps task records out of the timeline and says what a resumed run reused", () => {
    seq = 0;
    const s = reduceAll(emptyRun("r"), [
      ev("task.completed", { key: "plan||Planning|m1", ok: true, replayed: true, text: "{}" }),
      ev("run.resumed", { from_run_id: "old", tasks: 17, sources: 4, notes: 0, files: 2, legacy: false }),
    ]);
    expect(s.order).toEqual(["e2"]);
    render(<MemoryRouter><SimpleRow item={s.items.e2 as SimpleItem} onOpenArtifact={() => {}} /></MemoryRouter>);
    expect(screen.getByText(/17 finished tasks reused, 4 sources, 2 files carried over/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "the earlier run" })).toHaveAttribute("href", "/runs/old");
  });
});

describe("the budget reserve", () => {
  it("is a guardrail with a default of nothing", async () => {
    const onChange = vi.fn();
    render(<GuardrailsEditor value={{}} onChange={onChange} />);
    const field = screen.getByLabelText("Budget buffer");
    expect(field).toHaveValue("0");
    await userEvent.clear(field);
    await userEvent.type(field, "3");
    expect(onChange).toHaveBeenLastCalledWith({ budget: { buffer_usd: 3 } });
  });

  it("can be refilled, and the budget raised, on a live run", async () => {
    useRuns.getState().init("r1", []);
    useRuns.getState().apply("r1", [
      { ...ev("budget.update", { used: { usd: 4.2, tokens_in: 0, tokens_out: 0, steps: 3, runtime_s: 1 },
        limits: { usd: 5, tokens: null, steps: 80, runtime_s: 3600 }, buffer: { usd: 0, tokens: 0 } }), run_id: "r1", ephemeral: true },
    ]);
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(json({}, 202));
    wrap(<BudgetControl runId="r1" />);
    await userEvent.click(screen.getByRole("button", { name: "Budget" }));
    expect(screen.getByText(/\$4\.20 spent of \$5\.00/)).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Add to the budget now"), "2.5");
    await userEvent.type(screen.getByLabelText("Reserve"), "3");
    await userEvent.click(screen.getByRole("button", { name: "Apply" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(String(url)).toBe("/api/v1/runs/r1/budget");
    expect(JSON.parse(String(init?.body))).toEqual({ add_usd: 2.5, buffer_usd: 3 });
  });

  it("shows the reserve being drawn and the user's changes in the timeline", () => {
    seq = 0;
    const s = reduceAll(emptyRun("r"), [
      ev("guardrail.extended", { rule: "budget.usd", from: 1, to: 101, buffer: true }),
      ev("budget.adjusted", { "budget.usd": { from: 5, to: 7.5 }, buffer_usd: 3 }, { kind: "user", name: "You" }),
    ]);
    render(
      <MemoryRouter>
        <SimpleRow item={s.items.e1 as SimpleItem} onOpenArtifact={() => {}} />
        <SimpleRow item={s.items.e2 as SimpleItem} onOpenArtifact={() => {}} />
      </MemoryRouter>,
    );
    expect(screen.getByText(/drew the reserve: \$1\.00 → \$101\.00/)).toBeInTheDocument();
    expect(screen.getByText(/You changed the budget \$5\.00 → \$7\.50, reserve \$3\.00/)).toBeInTheDocument();
  });
});
