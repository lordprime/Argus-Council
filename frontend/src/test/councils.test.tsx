import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import axe from "axe-core";
import type { ReactNode } from "react";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import { previewResize, setOverride } from "@/features/councils/logic";
import type { Council, Member, RoleInfo } from "@/lib/types";
import CouncilBuilder from "@/routes/councils/CouncilBuilder";
import CouncilDetail from "@/routes/councils/CouncilDetail";
import CouncilsList from "@/routes/councils/CouncilsList";

afterEach(() => vi.restoreAllMocks());

// ------------------------------------------------------------------ fixtures
const role = (key: string, overseer = false): RoleInfo => ({
  key, title: key.charAt(0).toUpperCase() + key.slice(1), description: `${key} role`, overseer, default_tools: [`${key}_tool`],
});
const ROLES = {
  roles: [
    role("chair"), role("researcher"), role("analyst"), role("writer"), role("architect"), role("coder"), role("tester"),
    role("designer"), role("assistant"), role("critic", true), role("monitor", true), role("security", true),
  ],
  rosters: {
    general: ["chair", "analyst", "critic", "monitor", "writer", "researcher", "security", "designer"],
    research: ["chair", "researcher", "analyst", "critic", "writer", "monitor", "researcher", "security"],
    coding: ["chair", "architect", "coder", "tester", "security", "monitor", "critic", "coder", "designer"],
    chat: ["assistant", "monitor", "critic", "researcher"],
  },
};
const OVERSEERS = new Set(["critic", "monitor", "security"]);

const member = (id: string, r: string, position: number, name = role(r).title): Member => ({
  id, name, role: r, provider: "mock", model: "mock-1", system_prompt: "", temperature: 0.3, position, enabled: true,
  config: { tools: [], overseer: OVERSEERS.has(r) },
});

const COUNCIL: Council = {
  id: "c1", name: "Alpha team", description: "Market research", mode: "research", min_size: 2, max_size: 6,
  guardrails: {}, parent_id: null, created_at: "2026-10-01T00:00:00Z", updated_at: "2026-10-01T00:00:00Z",
  members: [member("m1", "chair", 0), member("m2", "researcher", 1), member("m3", "analyst", 2)],
};

type Handler = (body: unknown, url: string) => { status?: number; body?: unknown };

/** Route fetch calls by "METHOD /path" (query string ignored). */
function mockApi(routes: Record<string, Handler | unknown>) {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = String(input);
    const method = (init?.method ?? "GET").toUpperCase();
    const path = url.split("?")[0]!;
    const key = `${method} ${path}`;
    if (!(key in routes)) return new Response(JSON.stringify({ error: { code: "not_found", message: key } }), { status: 404, headers: { "content-type": "application/json" } });
    const h = routes[key];
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    const res = typeof h === "function" ? (h as Handler)(body, url) : { body: h };
    const status = res.status ?? 200;
    return status === 204
      ? new Response(null, { status })
      : new Response(JSON.stringify(res.body ?? {}), { status, headers: { "content-type": "application/json" } });
  });
}

const BASE = {
  "GET /api/v1/roles": ROLES,
  "GET /api/v1/providers": [],
  "GET /api/v1/providers/kinds": { kinds: [{ key: "mock", label: "Mock", protocol: "mock", default_base_url: null, requires_api_key: false, self_hosted: false, suggested_models: ["mock-1"] }], system_providers: ["mock"] },
  "GET /api/v1/runs": [],
};

function renderAt(path: string, ui: ReactNode, routePath: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path={routePath} element={ui} />
          <Route path="/councils/:councilId" element={<p>Detail page</p>} />
          <Route path="/councils" element={<p>List page</p>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const bodyOf = (spy: ReturnType<typeof mockApi>, key: string) => {
  const [method, path] = key.split(" ");
  const call = spy.mock.calls.find(([u, i]) => String(u).split("?")[0] === path && (i?.method ?? "GET") === method);
  return call ? JSON.parse(String(call[1]?.body)) : undefined;
};

// ------------------------------------------------------------------ pure logic
describe("council logic", () => {
  it("previews resize exactly like services/councils.py resize_council", () => {
    const grow = previewResize(COUNCIL.members, "research", 6, ROLES.rosters, OVERSEERS, ROLES.roles);
    // roster_for(research, 6) wants chair, researcher, analyst, critic, writer, monitor; first three are seated.
    expect(grow.added).toEqual([
      { role: "critic", name: "Critic" }, { role: "writer", name: "Writer" }, { role: "monitor", name: "Monitor" },
    ]);
    const shrink = previewResize(COUNCIL.members, "research", 2, ROLES.rosters, OVERSEERS, ROLES.roles);
    expect(shrink.removed.map((m) => m.id)).toEqual(["m3"]);
    // Roles the council lacks come first; repeated roles are numbered like the backend ("Researcher 2").
    const six = ["chair", "researcher", "analyst", "critic", "writer", "monitor"].map((r, i) => member(`s${i}`, r, i));
    expect(previewResize(six, "research", 7, ROLES.rosters, OVERSEERS, ROLES.roles).added).toEqual([{ role: "researcher", name: "Researcher 2" }]);
    const coders = [member("a", "coder", 0), member("b", "coder", 1, "Coder 2")];
    expect(previewResize(coders, "chat", 4, ROLES.rosters, OVERSEERS, ROLES.roles).added.map((a) => a.name)).toEqual(["Assistant", "Monitor"]);
  });

  it("keeps only changed guardrail keys", () => {
    let g = setOverride({}, ["budget", "usd"], 10);
    g = setOverride(g, ["max_steps"], 80); // equal to the default: not sent
    expect(g).toEqual({ budget: { usd: 10 } });
    expect(setOverride(g, ["budget", "usd"], 5)).toEqual({});
  });
});

// ------------------------------------------------------------------ list
describe("CouncilsList", () => {
  it("lists councils and deletes one only after typing its name", async () => {
    const spy = mockApi({ ...BASE, "GET /api/v1/councils": [COUNCIL], "DELETE /api/v1/councils/c1": () => ({ status: 204 }) });
    renderAt("/councils", <CouncilsList />, "/councils");
    const card = await screen.findByRole("article", { name: "Alpha team" });
    expect(within(card).getByText("Research")).toBeInTheDocument();
    expect(within(card).getByRole("img", { name: /critic|Chair, Chair/i })).toBeInTheDocument();
    await userEvent.click(within(card).getByRole("button", { name: "Delete Alpha team" }));
    const dialog = await screen.findByRole("dialog", { name: /Delete council “Alpha team”/ });
    const confirm = within(dialog).getByRole("button", { name: "Delete council" });
    expect(confirm).toBeDisabled();
    await userEvent.type(within(dialog).getByLabelText(/Type “Alpha team” to confirm/), "Alpha team");
    await userEvent.click(confirm);
    await waitFor(() => expect(spy).toHaveBeenCalledWith("/api/v1/councils/c1", expect.objectContaining({ method: "DELETE" })));
    await waitFor(() => expect(screen.queryByRole("article", { name: "Alpha team" })).not.toBeInTheDocument());
  });

  it("shows an empty state with a call to action", async () => {
    mockApi({ ...BASE, "GET /api/v1/councils": [] });
    renderAt("/councils", <CouncilsList />, "/councils");
    expect(await screen.findByText("No councils yet")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Create your first council" })).toHaveAttribute("href", "/councils/new");
  });
});

// ------------------------------------------------------------------ builder
describe("CouncilBuilder", () => {
  it("has no axe violations and sends only changed guardrails", async () => {
    const spy = mockApi({ ...BASE, "POST /api/v1/councils": (b: unknown) => ({ status: 201, body: { ...COUNCIL, ...(b as object), id: "new1" } }) });
    const { container } = renderAt("/councils/new", <CouncilBuilder />, "/councils/new");
    await screen.findByRole("list", { name: "Roster preview" });
    // Colour contrast needs a real layout engine; it is covered by the token contrast check and Playwright.
    const results = await axe.run(container, { rules: { "color-contrast": { enabled: false } } });
    expect(results.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target).join(", ")}`)).toEqual([]);
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);

    await userEvent.type(screen.getByLabelText("Name"), "Beta team");
    await userEvent.click(screen.getByRole("button", { name: "More seats" }));
    expect(within(screen.getByRole("list", { name: "Roster preview" })).getAllByRole("listitem")).toHaveLength(6);
    await userEvent.click(screen.getByRole("button", { name: "Customize guardrails" }));
    const budget = screen.getByLabelText("Budget");
    await userEvent.clear(budget);
    await userEvent.type(budget, "12.5");
    await userEvent.click(screen.getByRole("switch", { name: /File writes/ }));
    await userEvent.click(screen.getByRole("button", { name: "Create council" }));

    await screen.findByText("Detail page");
    const body = bodyOf(spy, "POST /api/v1/councils");
    expect(body).toMatchObject({ name: "Beta team", mode: "research", size: 6, min_size: 1, max_size: 12, default_provider: "mock", default_model: "mock-1" });
    expect(body.members).toBeUndefined();
    expect(body.guardrails).toEqual({
      budget: { usd: 12.5 },
      approval_required: ["shell", "package_install", "remote_agent", "network_write", "fs_write"],
    });
  });

  it("validates bounds client-side and maps a 409 to the name field", async () => {
    const spy = mockApi({ ...BASE, "POST /api/v1/councils": () => ({ status: 409, body: { error: { code: "conflict", message: "A council named 'Alpha team' already exists" } } }) });
    renderAt("/councils/new", <CouncilBuilder />, "/councils/new");
    await screen.findByRole("list", { name: "Roster preview" });
    await userEvent.type(screen.getByLabelText("Name"), "Alpha team");
    const min = screen.getByLabelText("Minimum seats");
    await userEvent.clear(min);
    await userEvent.type(min, "20");
    await userEvent.click(screen.getByRole("button", { name: "Create council" }));
    expect(await screen.findByText("Maximum must be at least the minimum.", { selector: "li" })).toBeInTheDocument();
    expect(spy.mock.calls.some(([, i]) => i?.method === "POST")).toBe(false);

    await userEvent.clear(min);
    await userEvent.type(min, "2");
    await userEvent.click(screen.getByRole("button", { name: "Create council" }));
    const name = screen.getByLabelText("Name");
    await waitFor(() => expect(name).toHaveAttribute("aria-invalid", "true"));
    expect(name).toHaveAccessibleDescription("A council named 'Alpha team' already exists");
  });
});

// ------------------------------------------------------------------ detail
describe("CouncilDetail", () => {
  it("previews a resize before applying it and reorders seats with Move buttons", async () => {
    let council = COUNCIL;
    const spy = mockApi({
      ...BASE,
      "GET /api/v1/councils/c1": () => ({ body: council }),
      "POST /api/v1/councils/c1/resize": (b: unknown) => {
        const size = (b as { size: number }).size;
        council = { ...council, members: [...council.members, member("m4", "critic", 3), member("m5", "writer", 4)].slice(0, size) };
        return { body: council };
      },
      "PATCH /api/v1/councils/c1/members/m1": (b: unknown) => {
        const { position } = b as { position: number };
        const others = council.members.filter((m) => m.id !== "m1");
        others.splice(position, 0, council.members.find((m) => m.id === "m1")!);
        council = { ...council, members: others.map((m, i) => ({ ...m, position: i })) };
        return { body: council.members[position] };
      },
    });
    renderAt("/councils/c1", <CouncilDetail />, "/councils/:councilId");
    expect(await screen.findByRole("heading", { level: 1, name: "Alpha team" })).toBeInTheDocument();

    const more = screen.getByRole("button", { name: "More seats" });
    await userEvent.click(more);
    await userEvent.click(more);
    const preview = screen.getByRole("list", { name: "Resize preview" });
    expect(preview).toHaveTextContent("Critic");
    expect(preview).toHaveTextContent("Writer");
    await userEvent.click(screen.getByRole("button", { name: "Resize to 5" }));
    await waitFor(() => expect(bodyOf(spy, "POST /api/v1/councils/c1/resize")).toEqual({ size: 5 }));
    expect(await screen.findByRole("tab", { name: "Seats (5)" })).toBeInTheDocument();

    expect(screen.getByRole("button", { name: "Move Chair up" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "Move Chair down" }));
    await waitFor(() => expect(bodyOf(spy, "PATCH /api/v1/councils/c1/members/m1")).toEqual({ position: 1 }));
    // Chair is now second (optimistically, then confirmed by the refetch).
    const seats = within(screen.getByRole("list", { name: "Seats in speaking order" })).getAllByRole("listitem");
    expect(seats[1]).toHaveAccessibleName(/^Chair, seat 2 of 5/);
  });
});
