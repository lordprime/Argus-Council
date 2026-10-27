import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import axe from "axe-core";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { McpServer, RemoteAgent } from "@/features/connections/queries";
import type { LedgerEntry, User } from "@/lib/types";
import Ledger from "@/routes/connections/Ledger";
import McpServers from "@/routes/connections/McpServers";
import RemoteAgents from "@/routes/connections/RemoteAgents";
import { useSession } from "@/stores/session";

const MEMBER: User & { role: string } = { id: "u1", email: "m@example.com", display_name: "Member", is_admin: false, role: "member", created_at: "2026-01-01T00:00:00Z" };
const ADMIN: User & { role: string } = { ...MEMBER, id: "u0", is_admin: false, role: "admin" };

beforeEach(() => useSession.setState({ user: MEMBER, status: "authenticated" }));
afterEach(() => vi.restoreAllMocks());

const json = (body: unknown, status = 200) =>
  new Response(status === 204 ? null : JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

function mockFetch(handler: (url: string, init: RequestInit) => Response | undefined) {
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

const bodyOf = (mock: ReturnType<typeof mockFetch>, url: string, method: string) =>
  JSON.parse(String(mock.mock.calls.find(([u, i]) => u === url && i?.method === method)?.[1]?.body)) as Record<string, unknown>;

async function noAxeViolations(container: HTMLElement) {
  const results = await axe.run(container);
  expect(results.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.html).join(" | ")}`)).toEqual([]);
}

const SERVER: McpServer = {
  id: "m1", name: "github", transport: "http", url: "https://mcp.example.com/mcp", command: [], header_names: [],
  tool_allow: ["*"], risk: ["network"], trust: "ask", require_approval: [], enabled: true,
  tools: [{ name: "create_issue", description: "Open an issue" }, { name: "search", description: "Search code" }],
  created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z",
};
const BLOCKED: McpServer = { ...SERVER, id: "m2", name: "sketchy", trust: "blocked", tools: [] };

describe("MCP server trust", () => {
  it("marks blocked servers as not attached to runs, hides stdio from members, and has no axe violations", async () => {
    mockFetch((url, init) => (url === "/api/v1/mcp-servers" && !init.method ? json({ servers: [SERVER, BLOCKED], stdio_enabled: true }) : undefined));
    const { container } = renderAt("/connections/mcp", <McpServers />);
    const blocked = await screen.findByRole("article", { name: "sketchy" });
    expect(within(blocked).getByText("Not attached to runs")).toBeInTheDocument();
    expect(within(blocked).getByText("Blocked")).toBeInTheDocument();
    const ask = screen.getByRole("article", { name: "github" });
    expect(within(ask).queryByText("Not attached to runs")).not.toBeInTheDocument();
    expect(within(ask).getByText("Ask")).toBeInTheDocument();
    // "ask" adds the approval tag to every tool's effective risk.
    expect(within(ask).getByRole("list", { name: "Risk tags for create_issue" })).toHaveTextContent("approval");
    await noAxeViolations(container);

    await userEvent.click(screen.getAllByRole("button", { name: "Add server" })[0]!);
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).queryByRole("radio", { name: /stdio/ })).not.toBeInTheDocument();
    expect(within(dialog).getByRole("radio", { name: "Ask" })).toBeChecked(); // new default
  });

  it("shows stdio to admins", async () => {
    useSession.setState({ user: ADMIN, status: "authenticated" });
    mockFetch((url, init) => (url === "/api/v1/mcp-servers" && !init.method ? json({ servers: [], stdio_enabled: true }) : undefined));
    renderAt("/connections/mcp", <McpServers />);
    await userEvent.click((await screen.findAllByRole("button", { name: "Add server" }))[0]!);
    expect(within(await screen.findByRole("dialog")).getByRole("radio", { name: /stdio/ })).toBeInTheDocument();
  });

  it("sends the trust change and always-ask tools in the PATCH body, warning when choosing trusted", async () => {
    const fetchMock = mockFetch((url, init) => {
      if (url === "/api/v1/mcp-servers" && !init.method) return json({ servers: [SERVER], stdio_enabled: false });
      if (url === "/api/v1/mcp-servers/m1" && init.method === "PATCH") return json({ ...SERVER, trust: "trusted" });
      if (url === "/api/v1/mcp-servers/m1/discover" && init.method === "POST") return json({ ...SERVER, server_info: { name: "gh-mcp", version: "1.2" } });
      return undefined;
    });
    const { baseElement } = renderAt("/connections/mcp", <McpServers />);
    await userEvent.click(within(await screen.findByRole("article", { name: "github" })).getByRole("button", { name: "Edit github" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).queryByText("Only trust servers you control")).not.toBeInTheDocument();
    const trusted = within(dialog).getByRole("radio", { name: "Trusted" });
    expect(trusted).toHaveAccessibleDescription(/without asking first/);
    await userEvent.click(trusted);
    expect(within(dialog).getByText("Only trust servers you control")).toBeInTheDocument();

    // Connection test = discovery with a summary.
    await userEvent.click(within(dialog).getByRole("button", { name: "Test connection" }));
    expect(await within(dialog).findByText(/^Connected · 2 tools · \d+ ms$/)).toBeInTheDocument();
    expect(within(dialog).getByText(/gh-mcp 1\.2/)).toBeInTheDocument();

    const askSwitch = within(dialog).getByRole("switch", { name: "Always ask before create_issue" });
    expect(askSwitch).toHaveAttribute("aria-checked", "false");
    await userEvent.click(askSwitch);
    expect(askSwitch).toHaveAttribute("aria-checked", "true");
    // Disallowing one tool under "*" keeps the others allowed explicitly.
    await userEvent.click(within(dialog).getByRole("checkbox", { name: "Allow search" }));
    expect(within(dialog).getByLabelText("Allowed tools")).toHaveValue("create_issue");

    await noAxeViolations(baseElement);
    await userEvent.click(within(dialog).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/v1/mcp-servers/m1", expect.objectContaining({ method: "PATCH" })));
    expect(bodyOf(fetchMock, "/api/v1/mcp-servers/m1", "PATCH")).toMatchObject({
      trust: "trusted", require_approval: ["create_issue"], tool_allow: ["create_issue"],
    });
  });

  it("reports a failed connection test", async () => {
    mockFetch((url, init) => {
      if (url === "/api/v1/mcp-servers" && !init.method) return json({ servers: [SERVER], stdio_enabled: false });
      if (url === "/api/v1/mcp-servers/m1/discover") return json({ error: { code: "upstream_error", message: "Could not connect: refused" } }, 502);
      return undefined;
    });
    renderAt("/connections/mcp", <McpServers />);
    await userEvent.click(await screen.findByRole("button", { name: "Test connection to github" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/Connection failed · \d+ ms/);
    expect(alert).toHaveTextContent("Could not connect: refused");
  });
});

const ACP: RemoteAgent = {
  id: "a1", name: "partner", protocol: "acp", url: "https://acp.example.com/acp", agent_name: "writer", description: "",
  enabled: true, has_token: false, requires_approval: true, created_at: "2026-01-01T00:00:00Z",
  card: { name: "writer", description: "Writes", metadata: { capabilities: [{ name: "Drafting" }] } },
};
const A2A: RemoteAgent = {
  ...ACP, id: "a2", name: "research", protocol: "a2a", url: "https://agent.example.com/a2a", agent_name: null, requires_approval: false,
  card: { name: "Research", skills: [{ id: "s1", name: "Search" }, { id: "s2", name: "Summarize" }], capabilities: { streaming: true, pushNotifications: false } },
};

describe("Remote agent approval", () => {
  it("edits the ACP agent name and approval, summarizes cards and reports refreshes", async () => {
    const fetchMock = mockFetch((url, init) => {
      if (url === "/api/v1/remote-agents" && !init.method) return json({ agents: [ACP, A2A] });
      if (url === "/api/v1/remote-agents/a1" && init.method === "PATCH") return json({ ...ACP, agent_name: "editor", card: {} });
      if (url === "/api/v1/remote-agents/a2/refresh") return json(A2A);
      return undefined;
    });
    const { container } = renderAt("/connections/agents", <RemoteAgents />);
    const a2a = await screen.findByRole("article", { name: "research" });
    expect(within(a2a).getByText("A2A")).toBeInTheDocument();
    expect(within(a2a).getByText("No approval")).toBeInTheDocument();
    expect(within(a2a).getByText("2 skills")).toBeInTheDocument();
    expect(within(a2a).getByRole("list", { name: "research capabilities" })).toHaveTextContent("streaming");
    expect(within(a2a).getByRole("list", { name: "research capabilities" })).not.toHaveTextContent("pushNotifications");
    await noAxeViolations(container);

    await userEvent.click(within(a2a).getByRole("button", { name: "Refresh card for research" }));
    expect(await within(a2a).findByText(/^Refreshed · \d+ ms$/)).toBeInTheDocument();
    expect(within(a2a).getByRole("status")).toHaveTextContent("2 skills, capabilities: streaming");

    const acp = screen.getByRole("article", { name: "partner" });
    expect(within(acp).getByText("Needs approval")).toBeInTheDocument();
    await userEvent.click(within(acp).getByRole("button", { name: "Edit partner" }));
    const dialog = await screen.findByRole("dialog");
    const nameField = within(dialog).getByLabelText("Agent name");
    expect(nameField).toHaveValue("writer");
    await userEvent.clear(nameField);
    await userEvent.type(nameField, "editor");
    await userEvent.click(within(dialog).getByRole("switch", { name: "Require approval before delegating" }));
    await userEvent.click(within(dialog).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/v1/remote-agents/a1", expect.objectContaining({ method: "PATCH" })));
    expect(bodyOf(fetchMock, "/api/v1/remote-agents/a1", "PATCH")).toMatchObject({ agent_name: "editor", requires_approval: false });
  });

  it("does not offer an agent name for A2A agents", async () => {
    const fetchMock = mockFetch((url, init) => {
      if (url === "/api/v1/remote-agents" && !init.method) return json({ agents: [A2A] });
      if (url === "/api/v1/remote-agents/a2" && init.method === "PATCH") return json(A2A);
      return undefined;
    });
    renderAt("/connections/agents", <RemoteAgents />);
    await userEvent.click(await screen.findByRole("button", { name: "Edit research" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).queryByLabelText("Agent name")).not.toBeInTheDocument();
    await userEvent.click(within(dialog).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/v1/remote-agents/a2", expect.objectContaining({ method: "PATCH" })));
    expect(bodyOf(fetchMock, "/api/v1/remote-agents/a2", "PATCH")).not.toHaveProperty("agent_name");
  });
});

const RUN = "11111111-2222-3333-4444-555555555555";
const entry = (seq: number, over: Partial<LedgerEntry> = {}): LedgerEntry => ({
  seq, ts: "2026-01-01T00:00:00Z", protocol: "mcp", direction: "outbound", peer: "github", method: "tools/call", status: "ok",
  latency_ms: 42, run_id: null, request_sha256: "a".repeat(64), response_sha256: "b".repeat(64),
  hash: `${seq}`.repeat(64).slice(0, 64), prev_hash: "0".repeat(64), detail: {}, ...over,
});

describe("Ledger purpose", () => {
  it("shows method and purpose chips, filters by purpose and by run", async () => {
    const fetchMock = mockFetch((url) => {
      if (url.startsWith("/api/v1/ledger")) {
        return json({
          entries: [
            entry(4, { protocol: "a2a", method: "agent-card", detail: { agent: "research", purpose: "refresh" } }),
            entry(3, { method: "tools/list", detail: { server: "github", purpose: "discover" } }),
            entry(2, { method: "initialize", detail: { server: "github", purpose: "discover" } }),
            entry(1, { method: "tools/call", run_id: RUN }),
          ],
        });
      }
      return undefined;
    });
    const { container } = renderAt("/connections/ledger", <Ledger />);
    const table = await screen.findByRole("table");
    expect(within(table).getAllByRole("row")).toHaveLength(5);
    expect(within(table).getByText("agent-card")).toBeInTheDocument();
    expect(within(table).getByText("Refresh")).toBeInTheDocument();
    expect(within(table).getAllByText("Discover")).toHaveLength(2);
    expect(within(table).getByRole("link", { name: "11111111" })).toHaveAttribute("href", `/runs/${RUN}`);
    await noAxeViolations(container);

    const purpose = screen.getByRole("radiogroup", { name: "Purpose" });
    await userEvent.click(within(purpose).getByRole("radio", { name: "Discover" }));
    expect(within(table).getAllByRole("row")).toHaveLength(3);
    expect(within(table).getByText("initialize")).toBeInTheDocument();
    expect(within(table).queryByText("agent-card")).not.toBeInTheDocument();

    await userEvent.click(within(purpose).getByRole("radio", { name: "Run" }));
    expect(within(screen.getByRole("table")).getAllByRole("row")).toHaveLength(2);

    await userEvent.selectOptions(screen.getByLabelText("Run"), RUN);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(`/api/v1/ledger?limit=100&run_id=${RUN}`, expect.anything()));
    expect(screen.getByText(/Showing calls for run/)).toBeInTheDocument();
  });
});
