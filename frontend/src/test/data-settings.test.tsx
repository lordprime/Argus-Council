import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import axe from "axe-core";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { filenameFrom } from "@/features/data/queries";
import type { User } from "@/lib/types";
import DataSettings from "@/routes/settings/Data";
import { useSession } from "@/stores/session";

const json = (body: unknown, status = 200) =>
  new Response(status === 204 ? null : JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

type Handler = (url: string, init: RequestInit) => Response | undefined;
function mockFetch(handler: Handler) {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) =>
    handler(String(input), init ?? {}) ?? json({ error: { code: "not_found", message: `unmocked ${String(input)}` } }, 404));
}

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/settings/data"]}>
        <Routes>
          <Route path="/settings/data" element={<DataSettings />} />
          <Route path="/login" element={<h1>Sign in</h1>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const ADMIN = { id: "u1", email: "ada@example.com", display_name: "Ada", is_admin: true, role: "admin", created_at: "2026-01-02T00:00:00Z" } as User;
const MEMBER = { ...ADMIN, id: "u2", is_admin: false, role: "member" } as User;

const POLICY = {
  policy: { run_days: 90, event_days: 30, audit_days: 365, ledger_days: 0, batch_size: 500, interval_s: 3600, notes: ["The protocol ledger is kept forever."] },
  last_run: null,
  enabled: true,
};
const PREVIEW = {
  dry_run: true, started_at: "2026-10-27T10:00:00Z", finished_at: "2026-10-27T10:00:01Z", duration_ms: 12,
  runs: 7, run_events: 1234, audit_events: 42, ledger_entries: 0, blob_dirs: 3, blob_bytes: 2 * 1024 * 1024,
};

async function noAxeViolations(container: HTMLElement) {
  const results = await axe.run(container);
  expect(results.violations.map((v) => v.id)).toEqual([]);
}

beforeEach(() => useSession.setState({ user: ADMIN, status: "authenticated" }));
afterEach(() => vi.restoreAllMocks());

describe("Settings → Data & privacy", () => {
  it("downloads the export as a file", async () => {
    const createUrl = vi.fn(() => "blob:argus-export");
    const revokeUrl = vi.fn();
    Object.assign(URL, { createObjectURL: createUrl, revokeObjectURL: revokeUrl });
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    useSession.setState({ user: MEMBER });
    const fetchMock = mockFetch((url, init) => {
      if (url === "/api/v1/me/export" && init.method === "POST")
        return new Response(new Blob(["PK zip"]), {
          status: 200,
          headers: { "content-type": "application/zip", "content-disposition": 'attachment; filename="argus-export-20261027.zip"' },
        });
      return undefined;
    });
    const { container } = renderPage();
    expect(screen.getByRole("heading", { level: 1, name: "Data & privacy" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Data retention" })).not.toBeInTheDocument();
    await noAxeViolations(container);

    await userEvent.click(screen.getByRole("button", { name: "Download export" }));
    await waitFor(() => expect(click).toHaveBeenCalled());
    expect(createUrl).toHaveBeenCalledTimes(1);
    const anchor = click.mock.contexts[0] as HTMLAnchorElement;
    expect(anchor.download).toBe("argus-export-20261027.zip");
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/me/export", expect.objectContaining({ method: "POST" }));
  });

  it("requires the typed phrase and password before deleting the account", async () => {
    let calls = 0;
    const fetchMock = mockFetch((url, init) => {
      if (url === "/api/v1/admin/retention") return json(POLICY);
      if (url === "/api/v1/me" && init.method === "DELETE") {
        calls += 1;
        const body = JSON.parse(String(init.body)) as { password: string };
        if (body.password !== "right password!") return json({ error: { code: "invalid_password", message: "Current password is incorrect" } }, 400);
        return json(null, 204);
      }
      return undefined;
    });
    renderPage();
    await userEvent.click(screen.getByRole("button", { name: "Delete account…" }));
    const dialog = await screen.findByRole("dialog", { name: "Delete your account?" });
    const submit = within(dialog).getByRole("button", { name: "Permanently delete account" });
    expect(submit).toBeDisabled();

    await userEvent.type(within(dialog).getByLabelText('Type "delete my account" to confirm'), "delete my acc");
    await userEvent.type(within(dialog).getByLabelText("Current password"), "wrong password!");
    expect(submit).toBeDisabled();
    await userEvent.type(within(dialog).getByLabelText('Type "delete my account" to confirm'), "ount");
    expect(submit).toBeEnabled();
    await noAxeViolations(dialog);

    await userEvent.click(submit);
    expect(await within(dialog).findByText("Current password is incorrect")).toBeInTheDocument();
    expect(useSession.getState().status).toBe("authenticated");

    const pw = within(dialog).getByLabelText("Current password");
    await userEvent.clear(pw);
    await userEvent.type(pw, "right password!");
    await userEvent.click(submit);
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
    expect(useSession.getState().status).toBe("anonymous");
    expect(calls).toBe(2);
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/me", expect.objectContaining({ method: "DELETE" }));
  });

  it("shows the last-admin conflict from the server", async () => {
    mockFetch((url, init) => {
      if (url === "/api/v1/admin/retention") return json(POLICY);
      if (url === "/api/v1/me" && init.method === "DELETE")
        return json({ error: { code: "conflict", message: "You are the last active admin" } }, 409);
      return undefined;
    });
    renderPage();
    await userEvent.click(screen.getByRole("button", { name: "Delete account…" }));
    const dialog = await screen.findByRole("dialog");
    await userEvent.type(within(dialog).getByLabelText('Type "delete my account" to confirm'), "delete my account");
    await userEvent.type(within(dialog).getByLabelText("Current password"), "pw");
    await userEvent.click(within(dialog).getByRole("button", { name: "Permanently delete account" }));
    expect(await within(dialog).findByText("You are the last active admin")).toBeInTheDocument();
  });

  it("lets admins preview and run retention", async () => {
    const posts: unknown[] = [];
    mockFetch((url, init) => {
      if (url === "/api/v1/admin/retention") return json(POLICY);
      if (url === "/api/v1/admin/retention/run" && init.method === "POST") {
        const body = JSON.parse(String(init.body)) as { dry_run: boolean };
        posts.push(body);
        return json({ ...PREVIEW, dry_run: body.dry_run });
      }
      return undefined;
    });
    const { container } = renderPage();
    const section = (await screen.findByRole("heading", { name: "Data retention" })).closest("section")!;
    expect(within(section).getByText("Deleted with their artifacts after 90 days")).toBeInTheDocument();
    expect(within(section).getByText("Kept forever")).toBeInTheDocument();
    expect(within(section).getByText("The protocol ledger is kept forever.")).toBeInTheDocument();
    await noAxeViolations(container);

    await userEvent.click(within(section).getByRole("button", { name: "Preview" }));
    const table = await within(section).findByRole("table", { name: "Would be removed now (preview)" });
    expect(within(table).getByRole("row", { name: "Runs 7" })).toBeInTheDocument();
    expect(within(table).getByRole("row", { name: "Artifact storage 2.0 MiB" })).toBeInTheDocument();
    expect(posts).toEqual([{ dry_run: true }]);
    await noAxeViolations(container);

    await userEvent.click(within(section).getByRole("button", { name: "Run now" }));
    const dialog = await screen.findByRole("dialog", { name: "Apply retention now?" });
    await userEvent.click(within(dialog).getByRole("button", { name: "Delete old data" }));
    await waitFor(() => expect(posts).toEqual([{ dry_run: true }, { dry_run: false }]));
  });

  it("parses the download filename", () => {
    expect(filenameFrom('attachment; filename="a b.zip"', "x")).toBe("a b.zip");
    expect(filenameFrom(null, "fallback.zip")).toBe("fallback.zip");
  });
});
