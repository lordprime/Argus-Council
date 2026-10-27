import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import axe from "axe-core";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import ApiKeys from "@/routes/settings/ApiKeys";
import Profile from "@/routes/settings/Profile";
import { useSession } from "@/stores/session";

const USER = { id: "u1", email: "ada@example.com", display_name: "Ada", is_admin: true, created_at: "2026-01-02T00:00:00Z" };
const PLAINTEXT = "argus_sk_live_9f8e7d6c5b4a3210";

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

function renderPage(path: string, ui: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  useSession.setState({ user: null, status: "authenticated" });
  localStorage.clear();
  delete document.documentElement.dataset.motion;
});
afterEach(() => vi.restoreAllMocks());

describe("Profile", () => {
  it("has no axe violations, one h1, and marks the active settings link", async () => {
    mockApi({ "GET /api/v1/auth/me": () => json(USER) });
    const { container } = renderPage("/settings/profile", <Profile />);
    expect(await screen.findByDisplayValue("ada@example.com")).toHaveAttribute("readonly");
    expect(screen.getByText("Admin")).toBeInTheDocument();
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    expect(screen.getByRole("link", { name: "Profile" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "API keys" })).not.toHaveAttribute("aria-current");
    const results = await axe.run(container);
    expect(results.violations.map((v) => v.id)).toEqual([]);
  });

  it("blocks a password change client-side when the confirmation does not match", async () => {
    const fetchMock = mockApi({ "GET /api/v1/auth/me": () => json(USER) });
    renderPage("/settings/profile", <Profile />);
    await userEvent.type(screen.getByLabelText("Current password"), "old-password-123");
    await userEvent.type(screen.getByLabelText("New password"), "brand-new-password-1");
    await userEvent.type(screen.getByLabelText("Confirm new password"), "brand-new-password-2");
    await userEvent.click(screen.getByRole("button", { name: "Change password" }));

    expect(screen.getByLabelText("Confirm new password")).toHaveAttribute("aria-invalid", "true");
    expect(screen.getAllByText("Passwords do not match.").length).toBeGreaterThan(0);
    expect(fetchMock.mock.calls.some(([, init]) => init?.method === "PATCH")).toBe(false);
  });

  it("maps a wrong current password from the server onto that field", async () => {
    mockApi({
      "GET /api/v1/auth/me": () => json(USER),
      "PATCH /api/v1/auth/me": () => json({ error: { code: "bad_request", message: "Current password is incorrect" } }, 400),
    });
    renderPage("/settings/profile", <Profile />);
    await userEvent.type(screen.getByLabelText("Current password"), "wrong-password-1");
    await userEvent.type(screen.getByLabelText("New password"), "brand-new-password-1");
    await userEvent.type(screen.getByLabelText("Confirm new password"), "brand-new-password-1");
    await userEvent.click(screen.getByRole("button", { name: "Change password" }));
    await waitFor(() => expect(screen.getByLabelText("Current password")).toHaveAttribute("aria-invalid", "true"));
    expect(screen.getAllByText("Current password is incorrect").length).toBeGreaterThan(0);
  });

  it("persists the reduce-motion override", async () => {
    mockApi({ "GET /api/v1/auth/me": () => json(USER) });
    renderPage("/settings/profile", <Profile />);
    await userEvent.click(screen.getByRole("switch", { name: /^Reduce motion/ }));
    expect(document.documentElement.dataset.motion).toBe("reduced");
    expect(localStorage.getItem("argus.motion")).toBe("reduced");
  });
});

describe("API keys", () => {
  it("shows the plaintext once and drops it after the acknowledgement", async () => {
    let listed: unknown[] = [];
    const created = {
      id: "k1", name: "Laptop", prefix: "argus_sk_li", scopes: ["*"], created_at: "2026-10-27T00:00:00Z",
      last_used_at: null, expires_at: null, revoked: false,
    };
    const fetchMock = mockApi({
      "GET /api/v1/auth/api-keys": () => json(listed),
      "POST /api/v1/auth/api-keys": () => {
        listed = [created];
        return json({ ...created, key: PLAINTEXT }, 201);
      },
    });
    renderPage("/settings/api-keys", <ApiKeys />);
    await screen.findByText("No API keys yet");
    await userEvent.click(screen.getByRole("button", { name: "Create key" }));

    const dialog = await screen.findByRole("dialog");
    await userEvent.type(within(dialog).getByLabelText("Name"), "Laptop");
    await userEvent.click(within(dialog).getByRole("radio", { name: /Full access/ }));
    await userEvent.click(within(dialog).getByRole("button", { name: "Create key" }));

    expect(await screen.findByDisplayValue(PLAINTEXT)).toBeInTheDocument();
    expect(screen.getByText(new RegExp(`--header 'Authorization: Bearer ${PLAINTEXT}'`))).toBeInTheDocument();
    const post = fetchMock.mock.calls.find(([, init]) => init?.method === "POST");
    expect(JSON.parse(String(post?.[1]?.body))).toMatchObject({ name: "Laptop", scopes: ["*"], expires_in_days: 90 });

    // Escape does not dismiss an unacknowledged key.
    await userEvent.keyboard("{Escape}");
    expect(screen.getByDisplayValue(PLAINTEXT)).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent(/stored the key/);

    const done = screen.getByRole("button", { name: "Done" });
    expect(done).toBeDisabled();
    await userEvent.click(screen.getByRole("checkbox", { name: /I've stored it/ }));
    await userEvent.click(done);

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(document.body.textContent).not.toContain(PLAINTEXT);
    expect(screen.queryByDisplayValue(PLAINTEXT)).toBeNull();
    expect(await screen.findByText("Laptop")).toBeInTheDocument();

    // Re-opening starts a fresh form; the old plaintext is gone for good.
    await userEvent.click(screen.getByRole("button", { name: "Create key" }));
    expect(await screen.findByLabelText("Name")).toHaveValue("");
    expect(document.body.textContent).not.toContain(PLAINTEXT);
  });

  it("names the key in the revoke confirmation", async () => {
    const key = {
      id: "k9", name: "CI pipeline", prefix: "argus_sk_ci", scopes: ["runs:write"], created_at: "2026-10-01T00:00:00Z",
      last_used_at: null, expires_at: null, revoked: false,
    };
    const fetchMock = mockApi({
      "GET /api/v1/auth/api-keys": () => json([key]),
      "DELETE /api/v1/auth/api-keys/k9": () => new Response(null, { status: 204 }),
    });
    renderPage("/settings/api-keys", <ApiKeys />);
    expect(await screen.findByText("Runs only")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Revoke CI pipeline" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("heading")).toHaveTextContent("Revoke “CI pipeline”?");
    await userEvent.click(within(dialog).getByRole("button", { name: "Revoke key" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/v1/auth/api-keys/k9", expect.objectContaining({ method: "DELETE" })));
  });
});
