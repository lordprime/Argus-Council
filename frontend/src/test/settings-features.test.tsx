import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import axe from "axe-core";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { FeatureOut, User } from "@/lib/types";
import ApiKeys from "@/routes/settings/ApiKeys";
import Features from "@/routes/settings/Features";
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

const BROWSER: FeatureOut = {
  key: "browser", label: "Live browser", installed: true, status: "reachable", enabled: true, active: true,
  install_command: "./scripts/deploy.sh --with browser", updated_at: null, updated_by: null,
};
const CLI: FeatureOut = {
  key: "cli_agents", label: "CLI agents", installed: false, status: "not_installed", enabled: true, active: false,
  install_command: "./scripts/deploy.sh --with cli-agents", updated_at: null, updated_by: null,
};

const FEATURES = "/api/v1/admin/features";
const card = (name: string) => screen.getByRole("region", { name });

beforeEach(() => useSession.setState({ user: ADMIN, status: "authenticated" }));
afterEach(() => vi.restoreAllMocks());

describe("Settings → Features", () => {
  it("renders one card per feature with its status badge", async () => {
    mockFetch((url) => (url === FEATURES ? json([BROWSER, CLI]) : undefined));
    const { container } = renderAt("/settings/features", <Features />);
    await screen.findByRole("switch", { name: "Enable Live browser" });

    expect(within(card("Live browser")).getByText("Installed and reachable")).toBeInTheDocument();
    expect(within(card("CLI agents")).getByText("Not installed")).toBeInTheDocument();
    expect(screen.getByText(/installed once on the server/)).toBeInTheDocument();
    // The browser card carries the residual-risk note and the threat model link.
    expect(within(card("Live browser")).getByText("--no-sandbox")).toBeInTheDocument();
    expect(within(card("Live browser")).getByRole("link", { name: "Threat model (TM-13)" }))
      .toHaveAttribute("href", expect.stringContaining("docs/security/threat-model.md"));
    expect(within(card("CLI agents")).getByText(/sign-ins and keys are kept/)).toBeInTheDocument();

    const results = await axe.run(container);
    expect(results.violations.map((v) => v.id)).toEqual([]);
  });

  it("shows the unreachable badge and a hint when the switch is on", async () => {
    mockFetch((url) => (url === FEATURES ? json([{ ...BROWSER, status: "unreachable" }, { ...CLI, installed: true, status: "unreachable", enabled: false, active: false }]) : undefined));
    renderAt("/settings/features", <Features />);
    await screen.findByRole("switch", { name: "Enable Live browser" });
    expect(within(card("Live browser")).getByText("Installed but unreachable")).toBeInTheDocument();
    expect(within(card("Live browser")).getByText(/the service isn't answering/)).toBeInTheDocument();
    // Off and unreachable: no hint.
    expect(within(card("CLI agents")).getByText("Installed but unreachable")).toBeInTheDocument();
    expect(within(card("CLI agents")).queryByText(/the service isn't answering/)).not.toBeInTheDocument();
    expect(screen.getByRole("switch", { name: "Enable CLI agents" })).toHaveAttribute("aria-checked", "false");
  });

  it("turns a feature off with PATCH {enabled:false} and shows who changed it", async () => {
    let resolvePatch: (r: Response) => void = () => undefined;
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === FEATURES) return json([BROWSER, CLI]);
      if (url === `${FEATURES}/browser` && init?.method === "PATCH") return new Promise<Response>((r) => { resolvePatch = r; });
      return json({ error: { code: "not_found", message: `unmocked ${url}` } }, 404);
    });
    renderAt("/settings/features", <Features />);
    const sw = await screen.findByRole("switch", { name: "Enable Live browser" });
    expect(sw).toHaveAttribute("aria-checked", "true");

    await userEvent.click(sw);
    await waitFor(() => expect(sw).toBeDisabled());
    const patch = fetchMock.mock.calls.find(([u, i]) => u === `${FEATURES}/browser` && i?.method === "PATCH");
    expect(JSON.parse(String(patch![1]!.body))).toEqual({ enabled: false });

    resolvePatch(json({ ...BROWSER, enabled: false, active: false, updated_at: new Date().toISOString(), updated_by: "ada@example.com" }));
    await waitFor(() => expect(sw).toHaveAttribute("aria-checked", "false"));
    expect(sw).toBeEnabled();
    expect(within(card("Live browser")).getByText(/Last changed/)).toHaveTextContent(/Last changed by ada@example.com/);
  });

  it("shows the exact install command and no switch when not installed", async () => {
    mockFetch((url) => (url === FEATURES ? json([BROWSER, CLI]) : undefined));
    renderAt("/settings/features", <Features />);
    await screen.findByRole("switch", { name: "Enable Live browser" });
    const cli = card("CLI agents");
    expect(within(cli).queryByRole("switch")).not.toBeInTheDocument();
    expect(within(cli).getByRole("region", { name: "CLI agents install command" })).toHaveTextContent("./scripts/deploy.sh --with cli-agents");
    expect(within(cli).getByRole("button", { name: "Copy CLI agents install command" })).toBeInTheDocument();
    expect(within(cli).getByText(/runs on the server, not in the app/)).toBeInTheDocument();
  });

  it("shows the API error from PATCH and keeps the switch as it was", async () => {
    mockFetch((url, init) => {
      if (url === FEATURES) return json([{ ...BROWSER, enabled: false, active: false }, CLI]);
      if (url === `${FEATURES}/browser` && init.method === "PATCH")
        return json({ error: { code: "conflict", message: "Live browser is not installed on this server: run ./scripts/deploy.sh --with browser" } }, 409);
      return undefined;
    });
    renderAt("/settings/features", <Features />);
    const sw = await screen.findByRole("switch", { name: "Enable Live browser" });
    await userEvent.click(sw);
    expect(await screen.findByRole("alert")).toHaveTextContent("Live browser is not installed on this server");
    expect(sw).toHaveAttribute("aria-checked", "false");
    expect(sw).toHaveAttribute("aria-invalid", "true");
  });
});

describe("Settings nav: Features", () => {
  it("shows Features to admins", () => {
    mockFetch((url) => (url === "/api/v1/auth/api-keys" ? json([]) : undefined));
    renderAt("/settings/api-keys", <ApiKeys />);
    expect(within(screen.getByRole("navigation", { name: "Settings" })).getByRole("link", { name: "Features" }))
      .toHaveAttribute("href", "/settings/features");
  });

  it("hides Features from non-admins and does not load the switches", () => {
    useSession.setState({ user: MEMBER, status: "authenticated" });
    const fetchMock = mockFetch((url) => (url === "/api/v1/auth/api-keys" ? json([]) : undefined));
    renderAt("/settings/api-keys", <ApiKeys />);
    expect(within(screen.getByRole("navigation", { name: "Settings" })).queryByRole("link", { name: "Features" })).not.toBeInTheDocument();

    renderAt("/settings/features", <Features />);
    expect(screen.getByText("Admins only")).toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([u]) => String(u).includes("/admin/features"))).toBe(false);
  });
});
