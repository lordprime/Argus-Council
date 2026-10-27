import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { NotificationBell } from "@/components/notifications/NotificationBell";
import { replayKey, replayStateAt, ReplayBar, stepDelay } from "@/components/run/ReplayBar";
import { RunTemplateForm } from "@/features/templates/components";
import { fillTemplate, templateVariables, type RunTemplate } from "@/features/templates/queries";
import type { RunEvent } from "@/lib/types";
import { emptyRun, useRuns } from "@/stores/run";

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

type Handler = (init: RequestInit | undefined) => Response;
function mockApi(routes: Record<string, Handler>) {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const key = `${init?.method ?? "GET"} ${String(input)}`;
    const handler = routes[key];
    if (!handler) throw new Error(`unmocked request: ${key}`);
    return handler(init);
  });
}

function renderUi(ui: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  localStorage.clear();
  useRuns.setState({ runs: {} });
});
afterEach(() => {
  vi.restoreAllMocks();
  delete document.documentElement.dataset.motion;
});

const TEMPLATE: RunTemplate = {
  id: "t1", name: "Market scan", summary: "", topic: "Market for {{product}} in {{region}}", description: "",
  mode: "research", council_id: null, size: null, provider: "mock", model: "mock-1", outputs: ["md"], guardrails: {},
  variables: ["product", "region"], use_count: 0, last_used_at: null,
  created_at: "2026-10-27T00:00:00Z", updated_at: "2026-10-27T00:00:00Z",
};

describe("template helpers", () => {
  it("extracts and fills variables", () => {
    expect(templateVariables("A {{ x }} and {{y}}", "{{x}} {{z_1}}")).toEqual(["x", "y", "z_1"]);
    expect(fillTemplate("Market for {{product}} in {{region}}", { product: "EVs" })).toBe("Market for EVs in {{region}}");
  });
});

describe("RunTemplateForm", () => {
  it("requires every variable, previews the topic, and posts the variables", async () => {
    const fetchMock = mockApi({
      "POST /api/v1/templates/t1/run": () => json({ id: "run-9", topic: "Market for EVs in Europe", status: "queued" }, 201),
    });
    renderUi(<RunTemplateForm template={TEMPLATE} />);
    await userEvent.click(screen.getByRole("button", { name: "Start run" }));
    expect(screen.getByLabelText("Product")).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByText("Region is required.")).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();

    await userEvent.type(screen.getByLabelText("Product"), "EVs");
    await userEvent.type(screen.getByLabelText("Region"), "Europe");
    expect(screen.getByTestId("template-preview")).toHaveTextContent("Market for EVs in Europe");
    await userEvent.click(screen.getByRole("button", { name: "Start run" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    const [, init] = fetchMock.mock.calls[0]!;
    expect(JSON.parse(String(init?.body))).toEqual({ variables: { product: "EVs", region: "Europe" } });
  });

  it("maps a server 422 for missing variables onto the field", async () => {
    mockApi({
      "POST /api/v1/templates/t1/run": () =>
        json({ error: { code: "invalid_variables", message: "Missing template variables: region", details: { missing: ["region"] } } }, 422),
    });
    renderUi(<RunTemplateForm template={{ ...TEMPLATE, variables: ["product", "region"] }} />);
    await userEvent.type(screen.getByLabelText("Product"), "EVs");
    await userEvent.type(screen.getByLabelText("Region"), " x");
    await userEvent.click(screen.getByRole("button", { name: "Start run" }));
    expect(await screen.findByText("Missing template variables: region")).toBeInTheDocument();
    expect(screen.getByText("Region is required.")).toBeInTheDocument();
  });
});

describe("NotificationBell", () => {
  const LIST = [
    { id: "n1", kind: "approval.requested", title: "Approval needed: Ship it", body: "deploy to prod", run_id: "r1",
      data: { approval_id: "apr_1" }, read_at: null, created_at: "2026-10-27T10:00:00Z" },
    { id: "n2", kind: "run.completed", title: "Run completed: Batteries", body: "", run_id: "r2",
      data: {}, read_at: null, created_at: "2026-10-27T09:00:00Z" },
  ];

  it("shows the unread badge, highlights approvals, links to runs and marks read", async () => {
    let unread = 2;
    const fetchMock = mockApi({
      "GET /api/v1/notifications/unread-count": () => json({ unread }),
      "GET /api/v1/notifications?limit=30": () => json({ notifications: LIST, unread }),
      "POST /api/v1/notifications/n1/read": () => {
        unread = 1;
        return json({ ...LIST[0], read_at: "2026-10-27T11:00:00Z" });
      },
    });
    renderUi(<NotificationBell />);
    expect(await screen.findByTestId("notification-badge")).toHaveTextContent("2");
    const bell = screen.getByRole("button", { name: "Notifications, 2 unread" });
    await userEvent.click(bell);

    const list = await screen.findByRole("list", { name: "Recent notifications" });
    const items = within(list).getAllByRole("listitem");
    expect(items[0]).toHaveAttribute("data-kind", "approval.requested");
    expect(within(items[0]!).getByText("Approval needed")).toBeInTheDocument();
    expect(within(items[0]!).getByRole("link")).toHaveAttribute("href", "/runs/r1");

    await userEvent.click(within(items[0]!).getByRole("button", { name: "Mark “Approval needed: Ship it” as read" }));
    await waitFor(() => expect(screen.getByTestId("notification-badge")).toHaveTextContent("1"));
    expect(fetchMock.mock.calls.some(([u, i]) => String(u) === "/api/v1/notifications/n1/read" && i?.method === "POST")).toBe(true);
  });

  it("marks everything read", async () => {
    let unread = 2;
    mockApi({
      "GET /api/v1/notifications/unread-count": () => json({ unread }),
      "GET /api/v1/notifications?limit=30": () =>
        json({ notifications: unread ? LIST : LIST.map((n) => ({ ...n, read_at: "2026-10-27T11:00:00Z" })), unread }),
      "POST /api/v1/notifications/read-all": () => {
        unread = 0;
        return json({ marked: 2, unread: 0 });
      },
    });
    renderUi(<NotificationBell />);
    await userEvent.click(await screen.findByRole("button", { name: "Notifications, 2 unread" }));
    await userEvent.click(await screen.findByRole("button", { name: "Mark all read" }));
    await waitFor(() => expect(screen.queryByTestId("notification-badge")).not.toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Notifications" })).toBeInTheDocument();
  });
});

describe("Run replay", () => {
  const actor = { kind: "user" as const, name: "You" };
  const ev = (seq: number, type: string, data: Record<string, unknown>, ts: string): RunEvent =>
    ({ v: 1, seq, id: `e${seq}`, run_id: "r1", ts, type, actor, data });
  const EVENTS: RunEvent[] = [
    ev(1, "run.status", { status: "running" }, "2026-10-27T10:00:00Z"),
    ev(2, "user.message", { text: "hello" }, "2026-10-27T10:00:01Z"),
    ev(3, "user.message", { text: "again" }, "2026-10-27T10:00:05Z"),
    ev(4, "run.status", { status: "completed" }, "2026-10-27T10:00:06Z"),
  ];

  it("reduces a prefix of the log with the shared reducer", () => {
    expect(replayStateAt("r1", [], EVENTS, 0).order).toEqual([]);
    const mid = replayStateAt("r1", [], EVENTS, 2);
    expect(mid.order).toEqual(["e1", "e2"]);
    expect(mid.status).toBe("running");
    expect(replayStateAt("r1", [], EVENTS, 4).status).toBe("completed");
    expect(stepDelay(EVENTS, 2, 1)).toBe(1000); // 4 s gap clamped to 1 s
    expect(stepDelay(EVENTS, 2, 8)).toBe(125);
  });

  it("steps forward and back through a separate replay state without touching the run", async () => {
    document.documentElement.dataset.motion = "reduced"; // no autoplay: stepping only
    useRuns.setState({ runs: { r1: { ...replayStateAt("r1", [], EVENTS, 4) } } });
    mockApi({ "GET /api/v1/runs/r1/events?after_seq=0&limit=2000": () => json({ events: EVENTS, has_more: false }) });
    renderUi(<ReplayBar runId="r1" members={[]} />);
    await userEvent.click(screen.getByRole("button", { name: "Replay run" }));

    const forward = await screen.findByRole("button", { name: "Step forward" });
    expect(screen.getByRole("button", { name: "Play replay" })).toHaveAttribute("aria-pressed", "false");
    const replay = () => useRuns.getState().runs[replayKey("r1")]!;
    expect(replay().order).toHaveLength(0);

    await userEvent.click(forward);
    await userEvent.click(forward);
    await userEvent.click(forward);
    expect(replay().order).toEqual(["e1", "e2", "e3"]);
    expect(screen.getByTestId("replay-position")).toHaveTextContent("3/4 · seq 3");

    await userEvent.click(screen.getByRole("button", { name: "Step back" }));
    expect(replay().order).toEqual(["e1", "e2"]);
    expect(replay().status).toBe("running");

    // Keyboard: "." steps forward from anywhere in the controls; the slider is labelled.
    const slider = screen.getByRole("slider", { name: "Replay position" });
    expect(slider).toHaveAttribute("aria-valuetext", "Event 2 of 4: user.message (seq 2)");
    act(() => slider.focus());
    await userEvent.keyboard(".");
    expect(replay().order).toHaveLength(3);

    // The real run state is untouched.
    expect(useRuns.getState().runs.r1!.order).toHaveLength(4);
    expect(useRuns.getState().runs.r1!.status).toBe("completed");

    await userEvent.click(screen.getByRole("button", { name: "2×" }));
    expect(screen.getByRole("button", { name: "2×" })).toHaveAttribute("aria-pressed", "true");

    await userEvent.click(screen.getByRole("button", { name: "Exit replay" }));
    expect(useRuns.getState().runs[replayKey("r1")]).toBeUndefined();
    expect(emptyRun("r1").order).toEqual([]);
  });
});
