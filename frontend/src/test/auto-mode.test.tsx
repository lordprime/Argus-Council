// Auto mode: the guardrails switches, the run store's `run.auto_mode` handling, auto-approved cards
// and the live control on a run.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { GuardrailsEditor } from "@/components/councils/GuardrailsEditor";
import { AutoModeControl } from "@/components/run/AutoModeControl";
import { ApprovalCard } from "@/components/run/parts";
import type { RunEvent } from "@/lib/types";
import { awaitsUser, emptyRun, reduceAll, useRuns, type ApprovalItem } from "@/stores/run";

let seq = 0;
const ev = (type: string, data: Record<string, unknown>, actor: RunEvent["actor"] = { kind: "system", name: "Argus" }): RunEvent => ({
  v: 1, seq: ++seq, id: `e${seq}`, run_id: "r", ts: "2026-11-01T00:00:00Z", type, actor, data,
});

afterEach(() => {
  vi.restoreAllMocks();
  useRuns.setState({ runs: {} });
});

describe("GuardrailsEditor auto mode", () => {
  it("sets auto_mode and extensions.auto_approve, and drops them again at the default", async () => {
    const onChange = vi.fn();
    const { rerender } = render(<GuardrailsEditor value={{}} onChange={onChange} />);
    const auto = screen.getByRole("switch", { name: "Auto mode" });
    expect(auto).toHaveAttribute("aria-checked", "false");
    expect(auto).toHaveAccessibleDescription(/without asking for approval\. Blocked actions stay blocked/);
    await userEvent.click(auto);
    expect(onChange).toHaveBeenLastCalledWith({ auto_mode: true });

    await userEvent.click(screen.getByRole("switch", { name: "Approve limit raises automatically" }));
    expect(onChange).toHaveBeenLastCalledWith({ extensions: { auto_approve: true } });

    rerender(<GuardrailsEditor value={{ auto_mode: true, extensions: { auto_approve: true } }} onChange={onChange} />);
    expect(screen.getByRole("switch", { name: "Auto mode" })).toHaveAttribute("aria-checked", "true");
    await userEvent.click(screen.getByRole("switch", { name: "Approve limit raises automatically" }));
    expect(onChange).toHaveBeenLastCalledWith({ auto_mode: true });
  });
});

describe("run store auto mode", () => {
  it("tracks run.auto_mode and marks auto-approved requests as never waiting for the user", () => {
    seq = 0;
    const s = reduceAll(emptyRun("r"), [
      ev("approval.requested", { approval_id: "a1", action_summary: "run shell", risk: ["shell"] }),
      ev("run.auto_mode", { tools: true, extensions: false }, { kind: "user", name: "You" }),
      ev("approval.resolved", { approval_id: "a1", decision: "approved", by: "auto" }),
      ev("approval.requested", { approval_id: "a2", action_summary: "deploy", risk: ["shell"], auto: true }),
    ]);
    expect(s.autoMode).toEqual({ tools: true, extensions: false });
    expect(s.items.a1).toMatchObject({ status: "approved", by: "auto" });
    // Asked under auto mode: not shown as waiting even before its resolution arrives.
    expect(awaitsUser(s.items.a2 as ApprovalItem)).toBe(false);
    expect(s.order).toContain("e2");
  });

  it("renders an auto-approved request as a record, without decision buttons", () => {
    const a: ApprovalItem = {
      kind: "approval", id: "a1", seq: 1, ts: "2026-11-01T00:00:00Z", actor: { kind: "agent", name: "Ana" },
      summary: "Ana wants to run shell", risk: ["shell"], status: "approved", by: "auto",
    };
    render(<ApprovalCard runId="r" a={a} />);
    expect(screen.getByText("Auto-approved")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
  });
});

describe("AutoModeControl", () => {
  it("starts from the run's guardrails and switches approvals on through the endpoint", async () => {
    const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ tools: true, extensions: true }), { status: 202, headers: { "content-type": "application/json" } }),
    );
    useRuns.getState().init("r1", []);
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <AutoModeControl runId="r1" guardrails={{ extensions: { enabled: true, auto_approve: true, limits: [], factor: 0.5, max_extensions: 3 } }} />
      </QueryClientProvider>,
    );
    await userEvent.click(screen.getByRole("button", { name: "Auto mode: on" }));
    expect(screen.getByRole("switch", { name: "Approve limit raises" })).toHaveAttribute("aria-checked", "true");
    const approvals = screen.getByRole("switch", { name: "Approve requests" });
    expect(approvals).toHaveAttribute("aria-checked", "false");
    await userEvent.click(approvals);
    await waitFor(() => expect(useRuns.getState().runs.r1?.autoMode).toEqual({ tools: true, extensions: true }));
    const [url, init] = fetch.mock.calls[0]!;
    expect(String(url)).toBe("/api/v1/runs/r1/auto-mode");
    expect(init?.method).toBe("POST");
    expect(JSON.parse(String(init?.body))).toEqual({ tools: true });
  });
});
