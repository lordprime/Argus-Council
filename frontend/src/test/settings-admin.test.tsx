import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import axe from "axe-core";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ProviderConfig, User } from "@/lib/types";
import Providers from "@/routes/connections/Providers";
import ApiKeys from "@/routes/settings/ApiKeys";
import Users from "@/routes/settings/Users";
import Webhooks from "@/routes/settings/Webhooks";
import { useSession } from "@/stores/session";

const json = (body: unknown, status = 200) =>
  new Response(status === 204 ? null : JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

type Handler = (url: string, init: RequestInit) => Response | undefined;
function mockFetch(handler: Handler) {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) =>
    handler(String(input), init ?? {}) ?? json({ error: { code: "not_found", message: `unmocked ${String(input)}` } }, 404));
}

function renderAt(path: string, ui: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

const ADMIN = { id: "u1", email: "ada@example.com", display_name: "Ada", is_admin: true, role: "admin", created_at: "2026-01-02T00:00:00Z" } as User;
const MEMBER = { ...ADMIN, id: "u2", email: "bob@example.com", display_name: "Bob", is_admin: false, role: "member" } as User;

const SECRET = "whsec_ONLY_SHOWN_ONCE_123";
const HOOK = {
  id: "h1", url: "https://hooks.example.com/argus", events: ["run.completed"], description: "CI relay", enabled: true,
  failure_count: 0, last_status: 200, last_delivery_at: "2026-10-27T10:00:00Z", created_at: "2026-10-01T00:00:00Z",
};
const EVENTS = { events: ["run.completed", "run.failed", "run.started"] };

beforeEach(() => useSession.setState({ user: ADMIN, status: "authenticated" }));
afterEach(() => vi.restoreAllMocks());

async function noAxeViolations(container: HTMLElement) {
  const results = await axe.run(container);
  expect(results.violations.map((v) => v.id)).toEqual([]);
}

describe("Settings → Webhooks", () => {
  it("shows the signing secret once after create and drops it after acknowledgement", async () => {
    let created = false;
    const fetchMock = mockFetch((url, init) => {
      if (url === "/api/v1/webhooks" && !init.method) return json({ webhooks: created ? [HOOK] : [] });
      if (url === "/api/v1/webhooks/events") return json(EVENTS);
      if (url === "/api/v1/webhooks" && init.method === "POST") {
        created = true;
        return json({ ...HOOK, secret: SECRET }, 201);
      }
      return undefined;
    });
    const { container } = renderAt("/settings/webhooks", <Webhooks />);
    expect(await screen.findByText("No webhooks yet")).toBeInTheDocument();
    await noAxeViolations(container);

    await userEvent.click(screen.getAllByRole("button", { name: "Add webhook" })[0]!);
    const dialog = await screen.findByRole("dialog", { name: "Add webhook" });
    await userEvent.type(within(dialog).getByLabelText("Payload URL"), HOOK.url);
    await userEvent.click(await within(dialog).findByRole("checkbox", { name: "run.completed" }));
    await userEvent.click(within(dialog).getByRole("button", { name: "Create webhook" }));

    const reveal = await screen.findByRole("dialog", { name: "Webhook created" });
    expect(within(reveal).getByDisplayValue(SECRET)).toHaveAttribute("readonly");
    expect(within(reveal).getByText("Copy this signing secret now")).toBeInTheDocument();
    expect(within(reveal).getByRole("button", { name: "Copy signing secret" })).toBeInTheDocument();
    const body = JSON.parse(String(fetchMock.mock.calls.find(([u, i]) => u === "/api/v1/webhooks" && i?.method === "POST")![1]!.body));
    expect(body).toMatchObject({ url: HOOK.url, events: ["run.completed"] });

    // Escape cannot dismiss an unacknowledged secret.
    await userEvent.keyboard("{Escape}");
    expect(screen.getByRole("dialog", { name: "Webhook created" })).toBeInTheDocument();
    expect(screen.getByText(/Confirm you've stored the secret/)).toBeInTheDocument();
    const done = within(reveal).getByRole("button", { name: "Done" });
    expect(done).toBeDisabled();
    await userEvent.click(within(reveal).getByRole("checkbox", { name: /I've stored it/ }));
    await userEvent.click(done);

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(await screen.findByRole("heading", { name: "hooks.example.com" })).toBeInTheDocument();
    expect(document.body.innerHTML).not.toContain(SECRET);
  });

  it("opens the recent deliveries drawer and reports a test result", async () => {
    mockFetch((url, init) => {
      if (url === "/api/v1/webhooks" && !init.method) return json({ webhooks: [HOOK] });
      if (url === "/api/v1/webhooks/h1/deliveries?limit=50") return json({ deliveries: [
        { id: 2, ts: "2026-10-27T10:00:00Z", event: "ping", delivery_id: "d2", attempt: 1, status_code: 200, ok: true, error: null, duration_ms: 87 },
        { id: 1, ts: "2026-10-27T09:00:00Z", event: "run.completed", delivery_id: "d1", attempt: 3, status_code: 500, ok: false, error: "HTTP 500", duration_ms: 1204 },
      ] });
      if (url === "/api/v1/webhooks/h1/test" && init.method === "POST") return json({ ...HOOK, ok: false, failure_count: 1, last_status: 503 });
      return undefined;
    });
    const { container } = renderAt("/settings/webhooks", <Webhooks />);
    await screen.findByRole("heading", { name: "hooks.example.com" });
    await noAxeViolations(container);

    await userEvent.click(screen.getByRole("button", { name: "Send test to hooks.example.com" }));
    expect(await screen.findByText("Test delivery failed")).toBeInTheDocument();
    expect(screen.getByText(/Failure count is now 1/)).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Recent deliveries for hooks.example.com" }));
    const drawer = await screen.findByRole("dialog", { name: "Recent deliveries" });
    const rows = await within(drawer).findAllByRole("row");
    expect(rows).toHaveLength(3);
    expect(within(rows[1]!).getByText("ping")).toBeInTheDocument();
    expect(within(rows[1]!).getByText("87 ms")).toBeInTheDocument();
    expect(within(rows[2]!).getByText("Failed")).toBeInTheDocument();
    expect(within(rows[2]!).getByText("HTTP 500", { selector: ".text-danger" })).toBeInTheDocument();
    expect(within(rows[2]!).getByText("3")).toBeInTheDocument();
    await noAxeViolations(drawer);
  });

  it("requires typing the name before deleting", async () => {
    const fetchMock = mockFetch((url, init) => {
      if (url === "/api/v1/webhooks" && !init.method) return json({ webhooks: [HOOK] });
      if (url === "/api/v1/webhooks/h1" && init.method === "DELETE") return json(null, 204);
      return undefined;
    });
    renderAt("/settings/webhooks", <Webhooks />);
    await userEvent.click(await screen.findByRole("button", { name: "Delete hooks.example.com" }));
    const dialog = await screen.findByRole("dialog", { name: "Delete webhook “hooks.example.com”?" });
    const confirm = within(dialog).getByRole("button", { name: "Delete hooks.example.com" });
    expect(confirm).toBeDisabled();
    await userEvent.type(within(dialog).getByLabelText("Type hooks.example.com to confirm"), "hooks.example.com");
    await userEvent.click(confirm);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/v1/webhooks/h1", expect.objectContaining({ method: "DELETE" })));
  });
});

describe("Settings → Users", () => {
  const USERS = [
    { id: "u1", email: "ada@example.com", display_name: "Ada", role: "admin", is_admin: true, is_active: true, created_at: "2026-01-02T00:00:00Z", last_login_at: null, run_count: 3 },
    { id: "u2", email: "bob@example.com", display_name: "Bob", role: "member", is_admin: false, is_active: true, created_at: "2026-02-02T00:00:00Z", last_login_at: null, run_count: 0 },
  ];

  it("shows the server's 409 message inline and keeps the previous role", async () => {
    const fetchMock = mockFetch((url, init) => {
      if (url === "/api/v1/admin/users") return json(USERS);
      if (url === "/api/v1/admin/users/u1" && init.method === "PATCH")
        return json({ error: { code: "conflict", message: "You cannot demote or deactivate your own account" } }, 409);
      return undefined;
    });
    const { container } = renderAt("/settings/users", <Users />);
    const select = await screen.findByRole("combobox", { name: "Role for ada@example.com" });
    await noAxeViolations(container);
    await userEvent.selectOptions(select, "viewer");
    expect(await screen.findByText("You cannot demote or deactivate your own account")).toBeInTheDocument();
    expect(select).toHaveValue("admin");
    expect(select).toHaveAttribute("aria-invalid", "true");
    const patch = fetchMock.mock.calls.find(([u, i]) => u === "/api/v1/admin/users/u1" && i?.method === "PATCH");
    expect(JSON.parse(String(patch![1]!.body))).toEqual({ role: "viewer" });
  });

  it("filters by search", async () => {
    mockFetch((url) => (url === "/api/v1/admin/users" ? json(USERS) : undefined));
    renderAt("/settings/users", <Users />);
    await screen.findByText("bob@example.com");
    await userEvent.type(screen.getByRole("searchbox", { name: "Search users" }), "bob");
    expect(screen.queryByText("ada@example.com")).not.toBeInTheDocument();
    expect(screen.getByText("bob@example.com")).toBeInTheDocument();
  });

  it("hides the Users nav item and the table from non-admins", async () => {
    useSession.setState({ user: MEMBER, status: "authenticated" });
    const fetchMock = mockFetch((url) => (url === "/api/v1/auth/api-keys" ? json([]) : undefined));
    renderAt("/settings/api-keys", <ApiKeys />);
    const nav = screen.getByRole("navigation", { name: "Settings" });
    expect(within(nav).getByRole("link", { name: "Webhooks" })).toBeInTheDocument();
    expect(within(nav).queryByRole("link", { name: "Users" })).not.toBeInTheDocument();

    renderAt("/settings/users", <Users />);
    expect(screen.getByText("Admins only")).toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([u]) => String(u).includes("/admin/users"))).toBe(false);
  });

  it("shows the Users nav item to admins (role or legacy is_admin)", () => {
    useSession.setState({ user: { ...MEMBER, role: undefined, is_admin: true } as User, status: "authenticated" });
    mockFetch((url) => (url === "/api/v1/auth/api-keys" ? json([]) : undefined));
    renderAt("/settings/api-keys", <ApiKeys />);
    expect(within(screen.getByRole("navigation", { name: "Settings" })).getByRole("link", { name: "Users" })).toBeInTheDocument();
  });
});

describe("Providers health", () => {
  const KINDS = {
    kinds: [{ key: "anthropic", label: "Anthropic", protocol: "anthropic", default_base_url: null, requires_api_key: true, self_hosted: false, suggested_models: ["claude-sonnet-4-5"] }],
    system_providers: [],
  };
  const base: ProviderConfig = {
    id: "p1", name: "anthropic", kind: "anthropic", base_url: null, default_model: "claude-sonnet-4-5", enabled: true, options: {},
    has_api_key: true, created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z",
  };

  it("renders the masked key hint and persisted health with icon, text, age and latency", async () => {
    const checked = new Date(Date.now() - 3 * 60_000).toISOString();
    mockFetch((url) => {
      if (url === "/api/v1/providers/kinds") return json(KINDS);
      if (url === "/api/v1/providers") return json([{
        ...base, key_hint: "sk-ant-…abcd", health_status: "auth_failed", health_detail: "401 invalid x-api-key",
        health_checked_at: checked, health_latency_ms: 412,
      }]);
      return undefined;
    });
    const { container } = renderAt("/connections/providers", <Providers />);
    const card = await screen.findByRole("article", { name: "anthropic" });
    expect(within(card).getByText("sk-ant-…abcd")).toBeInTheDocument();
    expect(within(card).queryByText("Stored · write-only")).not.toBeInTheDocument();
    const badge = within(card).getByText("Auth failed");
    expect(badge.querySelector("svg")).not.toBeNull();
    expect(within(card).getByText(/3 minutes ago/)).toBeInTheDocument();
    expect(within(card).getByText(/412 ms/)).toBeInTheDocument();
    expect(within(card).getByText("401 invalid x-api-key")).toBeInTheDocument();
    await noAxeViolations(container);
  });

  it("uses the persisting test endpoint and refetches providers afterwards", async () => {
    let tested = false;
    const fetchMock = mockFetch((url, init) => {
      if (url === "/api/v1/providers/kinds") return json(KINDS);
      if (url === "/api/v1/providers") return json([tested ? { ...base, health_status: "ok", health_checked_at: new Date().toISOString(), health_latency_ms: 120 } : base]);
      if (url === "/api/v1/providers/anthropic/test" && init.method === "POST") {
        tested = true;
        return json({ provider: "anthropic", ok: true, status: "ok", latency_ms: 120, detail: "12 models available", probe: "list_models", persisted: true, checked_at: new Date().toISOString() });
      }
      return undefined;
    });
    renderAt("/connections/providers", <Providers />);
    const card = await screen.findByRole("article", { name: "anthropic" });
    expect(within(card).getByText("Not tested")).toBeInTheDocument();
    await userEvent.click(within(card).getByRole("button", { name: "Test connection" }));
    await waitFor(() => expect(within(card).getByText(/120 ms/, { selector: "span" })).toBeInTheDocument());
    expect(within(card).getByText("Connected", { selector: "span" })).toBeInTheDocument();
    expect(fetchMock.mock.calls.filter(([u]) => u === "/api/v1/providers").length).toBeGreaterThanOrEqual(2);
    expect(fetchMock.mock.calls.some(([u]) => u === "/api/v1/llm/complete")).toBe(false);
  });
});
