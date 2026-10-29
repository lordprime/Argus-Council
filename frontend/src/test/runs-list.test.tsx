import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Run } from "@/lib/types";
import RunsList from "@/routes/RunsList";

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

function run(id: string, topic: string, status: Run["status"], mode: Run["mode"] = "research"): Run {
  return {
    id, topic, status, mode, council_id: null, parent_run_id: null, description: "", outputs: ["md"],
    guardrails: {} as Run["guardrails"], usage: { usd: 0.42, runtime_s: 95 }, error: null, last_seq: 10,
    created_at: "2026-10-27T10:00:00Z", started_at: "2026-10-27T10:00:01Z", finished_at: null,
  };
}

const RUNS = [
  run("r1", "Solid-state batteries", "completed"),
  run("r2", "CSV to JSON CLI", "failed", "coding"),
  run("r3", "Rust ownership", "cancelled", "chat"),
  run("r4", "Battery recycling", "running"),
];

function Probe() {
  const loc = useLocation();
  return <output data-testid="location">{loc.pathname + loc.search}</output>;
}

function renderRuns(path = "/runs") {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/runs" element={<RunsList />} />
          <Route path="/" element={<p>Home page</p>} />
        </Routes>
        <Probe />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function mockRuns(runs: Run[] = RUNS) {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = String(input);
    if ((init?.method ?? "GET") === "DELETE") return new Response(null, { status: 204 });
    if (url.startsWith("/api/v1/runs")) return json(runs);
    throw new Error(`unmocked ${url}`);
  });
}

function desktop() {
  const real = window.matchMedia;
  vi.spyOn(window, "matchMedia").mockImplementation((q: string) => ({ ...real(q), matches: q.includes("min-width: 640px") }));
}

afterEach(() => vi.restoreAllMocks());

describe("Runs list", () => {
  it("encodes filters in the URL and refetches with them", async () => {
    const fetchMock = mockRuns();
    renderRuns();
    expect(await screen.findByText("Solid-state batteries")).toBeInTheDocument();

    await userEvent.selectOptions(screen.getByLabelText("Status"), "failed");
    expect(screen.getByTestId("location")).toHaveTextContent("/runs?status=failed");
    await waitFor(() => expect(fetchMock.mock.calls.some(([u]) => String(u).includes("status=failed"))).toBe(true));

    await userEvent.selectOptions(screen.getByLabelText("Mode"), "coding");
    expect(screen.getByTestId("location")).toHaveTextContent("status=failed&mode=coding");
    await userEvent.click(screen.getByRole("button", { name: "Clear" }));
    expect(screen.getByTestId("location")).toHaveTextContent(/^\/runs$/);
  });

  it("filters topics client-side from the q param", async () => {
    mockRuns();
    renderRuns("/runs?q=batter");
    expect(await screen.findByText("Solid-state batteries")).toBeInTheDocument();
    expect(screen.getByText("Battery recycling")).toBeInTheDocument();
    expect(screen.queryByText("Rust ownership")).toBeNull();
    expect(screen.getByLabelText("Search topics")).toHaveValue("batter");

    await userEvent.clear(screen.getByLabelText("Search topics"));
    await userEvent.type(screen.getByLabelText("Search topics"), "rust");
    expect(screen.getByTestId("location")).toHaveTextContent("/runs?q=rust");
    expect(screen.getByText("Rust ownership")).toBeInTheDocument();
    expect(screen.queryByText("Battery recycling")).toBeNull();
  });

  it("moves only finished runs to the trash, and archives from the main list", async () => {
    desktop();
    const fetchMock = mockRuns();
    renderRuns();
    const table = await screen.findByRole("table");
    expect(within(table).getByRole("checkbox", { name: "Select “Battery recycling”" })).toBeDisabled();

    await userEvent.click(within(table).getByRole("checkbox", { name: "Select all finished runs" }));
    expect(screen.getByText("3 selected")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Move to trash" }));
    await waitFor(() => {
      const posts = fetchMock.mock.calls.filter(([, init]) => init?.method === "POST").map(([u]) => String(u));
      expect(posts.sort()).toEqual(["/api/v1/runs/r1/trash", "/api/v1/runs/r2/trash", "/api/v1/runs/r3/trash"]);
    });
    expect(fetchMock.mock.calls.some(([, init]) => init?.method === "DELETE")).toBe(false);

    await userEvent.click(within(table).getByRole("checkbox", { name: "Select “Solid-state batteries”" }));
    await userEvent.click(screen.getByRole("button", { name: "Archive" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/v1/runs/r1/archive", expect.objectContaining({ method: "POST" })));
  });

  it("deletes forever only from the trash view, naming the count in the confirmation", async () => {
    desktop();
    const fetchMock = mockRuns();
    renderRuns("/runs?view=trash");
    const table = await screen.findByRole("table");
    await waitFor(() => expect(fetchMock.mock.calls.some(([u]) => String(u).includes("view=trash"))).toBe(true));
    expect(screen.getByRole("tab", { name: "Trash" })).toHaveAttribute("aria-selected", "true");

    await userEvent.click(within(table).getByRole("checkbox", { name: "Select all finished runs" }));
    expect(screen.getByRole("button", { name: "Restore" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Delete forever" }));

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("heading")).toHaveTextContent("Delete 3 runs?");
    expect(within(dialog).queryByText("Battery recycling")).toBeNull();
    await userEvent.click(within(dialog).getByRole("button", { name: "Delete 3 runs" }));

    await waitFor(() => {
      const deletes = fetchMock.mock.calls.filter(([, init]) => init?.method === "DELETE").map(([u]) => String(u));
      expect(deletes.sort()).toEqual(["/api/v1/runs/r1", "/api/v1/runs/r2", "/api/v1/runs/r3"]);
    });
  });

  it("shows an empty state that links to Home", async () => {
    mockRuns([]);
    renderRuns();
    expect(await screen.findByText("No runs yet")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("link", { name: "Start a run" }));
    expect(await screen.findByText("Home page")).toBeInTheDocument();
  });

  it("renders an error state with retry", async () => {
    let fail = true;
    vi.spyOn(globalThis, "fetch").mockImplementation(async () =>
      fail ? json({ error: { code: "internal", message: "Database unavailable" } }, 500) : json(RUNS));
    renderRuns();
    expect(await screen.findByText("Database unavailable")).toBeInTheDocument();
    fail = false;
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("Rust ownership")).toBeInTheDocument();
  });
});
