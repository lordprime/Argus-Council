import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import axe from "axe-core";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import { auditCsv, bucketRuns } from "@/features/insights/logic";
import type { AuditEvent, Insights as InsightsData } from "@/features/insights/queries";
import type { Run } from "@/lib/types";
import Audit from "@/routes/insights/Audit";
import Insights from "@/routes/insights/Insights";
import { useSession } from "@/stores/session";

// axe over a 50-row table plus typing is slow on a loaded CI box.
vi.setConfig({ testTimeout: 20_000 });

afterEach(() => {
  vi.restoreAllMocks();
  useSession.setState({ user: null, status: "unknown" });
});

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

function mockFetch(handler: (url: string, init: RequestInit) => Response | undefined) {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) =>
    handler(String(input), init ?? {}) ?? json({ error: { code: "not_found", message: `unmocked ${String(input)}` } }, 404));
}

function renderAt(path: string, ui: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

const INSIGHTS = (days: number, runs = 12): InsightsData => ({
  window_days: days,
  analysed_runs: runs,
  totals: { runs, cost_usd: 3.5, tokens_in: 12000, tokens_out: 3000, avg_runtime_s: 42.5, success_rate: 75 },
  runs_by_status: { completed: 9, failed: 2, halted: 1 },
  runs_by_mode: { debate: 7, review: 5 },
  models: [
    { model: "anthropic/claude-sonnet-4-5", calls: 40, cost_usd: 3, tokens: 14000, avg_latency_ms: 900 },
    { model: "mock/mock-1", calls: 5, cost_usd: 0.5, tokens: 1000, avg_latency_ms: 20 },
  ],
  tools: [{ tool: "web.search", calls: 10, error_rate: 30, blocked: 1, avg_ms: 400 }],
  oversight: { intercepts_by_reason: { pii: 3, cost: 1 }, intercepts_by_action: { pause: 4 }, approvals: { approved: 2, rejected: 1 } },
  guardrail_violations: { budget_usd: 2, pii_detected: 1 },
  top_failures: [{ reason: "BudgetExceeded", count: 2 }],
});

const now = Date.now();
const RUN = (i: number, status: Run["status"] = "completed"): Run => ({
  id: `r${i}`, council_id: null, parent_run_id: null, topic: `Topic ${i}`, description: "", mode: "debate" as Run["mode"], status,
  outputs: [], guardrails: {} as Run["guardrails"], usage: { usd: 0.25, tokens_in: 100, tokens_out: 50, runtime_s: 30 },
  error: status === "halted" ? "budget_usd: limit reached" : null, last_seq: 0,
  created_at: new Date(now - i * 3_600_000).toISOString(), started_at: null, finished_at: null,
});

function mockDashboard() {
  return mockFetch((url) => {
    const m = url.match(/^\/api\/v1\/insights\?days=(\d+)$/);
    if (m) return json(INSIGHTS(Number(m[1]), Number(m[1]) === 30 ? 99 : 12));
    if (url === "/api/v1/runs?limit=200") return json([RUN(1), RUN(2, "halted"), RUN(30, "failed")]);
    if (url === "/api/v1/providers") return json([{ id: "p1", name: "anthropic", kind: "anthropic", enabled: true, health_status: "ok", health_latency_ms: 321 }]);
    return undefined;
  });
}

describe("Insights dashboard", () => {
  it("renders KPIs, charts with table views and has no axe violations", async () => {
    mockDashboard();
    const { container } = renderAt("/insights", <Insights />);
    expect(await screen.findByTestId("kpi-Runs")).toHaveTextContent("12");
    expect(screen.getByTestId("kpi-Success rate")).toHaveTextContent("75%");
    expect(screen.getByTestId("kpi-Spend")).toHaveTextContent("$3.50");
    expect(screen.getByTestId("kpi-Tokens")).toHaveTextContent("15k");
    expect(screen.getByTestId("kpi-Avg runtime")).toHaveTextContent("42.5s");
    expect(screen.getByRole("heading", { level: 1, name: "Council insights" })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: /Spend by model/ })).toBeInTheDocument();
    // Provider health probe latency is preferred over the average call latency.
    expect(await screen.findByText("321 ms")).toBeInTheDocument();
    // One Insights section (C-10, C-27): eyebrow, h1, section tabs; page actions in the header, no quick-action tiles
    // and no in-page breadcrumb (the shell header carries the breadcrumb).
    const nav = screen.getByRole("navigation", { name: "Insights" });
    expect(within(nav).getByRole("link", { name: "Overview" })).toHaveAttribute("aria-current", "page");
    expect(within(nav).getByRole("link", { name: "Audit log" })).toHaveAttribute("href", "/insights/audit");
    expect(screen.getByRole("button", { name: "Export JSON" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Refresh" })).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Quick actions" })).toBeNull();
    expect(screen.queryByRole("navigation", { name: "Breadcrumb" })).toBeNull();
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    // Health labels are sentence-case strings ("OK", "Not probed"), not CSS-capitalised Title Case.
    expect(screen.getByText("Not probed")).not.toHaveClass("capitalize");

    const results = await axe.run(container);
    expect(results.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.html).join(" | ")}`)).toEqual([]);

    const spend = screen.getByRole("region", { name: "Spend by model" });
    await userEvent.click(within(spend).getByRole("button", { name: "View as table" }));
    const table = within(spend).getByRole("table");
    expect(within(table).getByRole("rowheader", { name: "anthropic/claude-sonnet-4-5" })).toBeInTheDocument();
    expect(within(spend).getByRole("button", { name: "View as chart" })).toHaveAttribute("aria-pressed", "true");
  });

  it("steps through chart points with the keyboard and shows a tooltip", async () => {
    mockDashboard();
    renderAt("/insights", <Insights />);
    const slider = await screen.findByRole("slider", { name: /Spend by model.*explore bars/ });
    act(() => slider.focus());
    expect(slider).toHaveAttribute("aria-valuetext", "anthropic/claude-sonnet-4-5: $3.00, 40 calls · 14k tokens");
    await userEvent.keyboard("{ArrowRight}");
    expect(slider).toHaveAttribute("aria-valuetext", expect.stringContaining("mock/mock-1: $0.5000"));
  });

  it("refetches with the days param when the range changes", async () => {
    const fetchMock = mockDashboard();
    renderAt("/insights", <Insights />);
    await screen.findByTestId("kpi-Runs");
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/insights?days=7", expect.anything());
    await userEvent.click(screen.getByRole("radio", { name: "30D" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/v1/insights?days=30", expect.anything()));
    await waitFor(() => expect(screen.getByTestId("kpi-Runs")).toHaveTextContent("99"));
    await userEvent.click(screen.getByRole("radio", { name: "1D" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/v1/insights?days=1", expect.anything()));
  });
});

describe("insights logic", () => {
  it("buckets runs hourly for one day and daily otherwise", () => {
    const runs = [RUN(1), RUN(2, "halted"), RUN(30, "failed")];
    const h = bucketRuns(runs, 1, now);
    expect(h).toHaveLength(24);
    expect(h.reduce((s, b) => s + b.runs, 0)).toBe(2);
    const d = bucketRuns(runs, 7, now);
    expect(d).toHaveLength(7);
    expect(d.reduce((s, b) => s + b.runs, 0)).toBe(3);
    expect(d.reduce((s, b) => s + b.failed, 0)).toBe(2);
  });

  it("escapes CSV cells", () => {
    const csv = auditCsv([{ ...EVENT(1), detail: { note: 'a "b", c' } }]);
    expect(csv.split("\r\n")[1]).toContain('"{""note"":""a \\""b\\"", c""}"');
  });
});

const EVENT = (id: number, over: Partial<AuditEvent> = {}): AuditEvent => ({
  id, ts: new Date(now - id * 60_000).toISOString(), actor_id: "u1-aaaa-bbbb", action: id % 2 ? "auth.login" : "provider.update",
  target_type: "user", target_id: "u1", success: id % 3 !== 0, ip: "10.0.0.1", user_agent: "test", detail: { n: id }, ...over,
});

describe("Audit log", () => {
  it("filters, loads more via before_id, expands detail and has no axe violations", async () => {
    useSession.setState({ user: { id: "u1", email: "a@b.c", display_name: "A", is_admin: true, created_at: "" }, status: "authenticated" });
    const fetchMock = mockFetch((url) => {
      if (!url.startsWith("/api/v1/audit?")) return undefined;
      const qs = new URL(url, "http://x").searchParams;
      const before = Number(qs.get("before_id") ?? 0);
      if (before) return json({ events: [EVENT(before + 100)] });
      return json({ events: Array.from({ length: 50 }, (_, i) => EVENT(200 - i)) });
    });
    const { container } = renderAt("/insights/audit", <Audit />);
    const region = await screen.findByRole("region", { name: "Audit events" });
    expect(within(region).getAllByRole("row")).toHaveLength(51);
    expect(screen.getByText("50 events loaded")).toBeInTheDocument();

    const results = await axe.run(container);
    expect(results.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.html).join(" | ")}`)).toEqual([]);

    await userEvent.click(screen.getByRole("button", { name: "Load more" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/v1/audit?before_id=151&limit=50", expect.anything()));
    expect(await screen.findByText("51 events loaded")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Load more" })).toBeNull();

    await userEvent.click(screen.getByRole("button", { name: "Show details for event 200" }));
    expect(screen.getByText(/"n": 200/)).toBeInTheDocument();

    // Filters apply live (C-28): no Apply button; the typed action is debounced.
    expect(screen.queryByRole("button", { name: "Apply" })).toBeNull();
    expect(within(screen.getByRole("navigation", { name: "Insights" })).getByRole("link", { name: "Audit log" }))
      .toHaveAttribute("aria-current", "page");
    await userEvent.type(screen.getByLabelText("Action contains"), "auth");
    await userEvent.selectOptions(screen.getByLabelText("Result"), "false");
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/v1/audit?action=auth&success=false&limit=50", expect.anything()));

    await userEvent.click(screen.getByRole("switch", { name: "All users" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/v1/audit?action=auth&success=false&all=true&limit=50", expect.anything()));
    expect(await screen.findByRole("columnheader", { name: "Actor" })).toBeInTheDocument();
  });

  it("hides the all-users toggle from non-admins", async () => {
    useSession.setState({ user: { id: "u2", email: "x@y.z", display_name: "X", is_admin: false, created_at: "" }, status: "authenticated" });
    mockFetch((url) => (url.startsWith("/api/v1/audit?") ? json({ events: [] }) : undefined));
    renderAt("/insights/audit", <Audit />);
    expect(await screen.findByText("No audit events yet")).toBeInTheDocument();
    expect(screen.queryByRole("switch", { name: "All users" })).toBeNull();
  });
});
