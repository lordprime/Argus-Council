import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import axe from "axe-core";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { LedgerEntry, McpServerInfo, ProviderConfig } from "@/lib/types";
import Ledger from "@/routes/connections/Ledger";
import McpServers from "@/routes/connections/McpServers";
import Providers from "@/routes/connections/Providers";
import RemoteAgents from "@/routes/connections/RemoteAgents";

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

const SECRET = "sk-ant-SUPERSECRET-abcd";
const KINDS = {
  kinds: [
    { key: "anthropic", label: "Anthropic", protocol: "anthropic", default_base_url: null, requires_api_key: true, self_hosted: false, suggested_models: ["claude-sonnet-4-5"] },
    { key: "ollama", label: "Ollama", protocol: "openai", default_base_url: "http://localhost:11434/v1", requires_api_key: false, self_hosted: true, suggested_models: [] },
  ],
  system_providers: ["mock"],
};
const PROVIDER: ProviderConfig & { api_key?: string } = {
  id: "p1", name: "anthropic", kind: "anthropic", base_url: null, default_model: "claude-sonnet-4-5", enabled: true, options: {},
  has_api_key: true, created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z",
  // A misbehaving backend leaking the key must still not reach the DOM: the UI only reads has_api_key.
  api_key: SECRET,
};

describe("Providers", () => {
  it("never renders a stored secret back, keeps the key write-only when editing, and has no axe violations", async () => {
    const fetchMock = mockFetch((url, init) => {
      if (url === "/api/v1/providers/kinds") return json(KINDS);
      if (url === "/api/v1/providers" && !init.method) return json([PROVIDER]);
      if (url === "/api/v1/providers/p1" && init.method === "PATCH") return json({ ...PROVIDER, api_key: undefined });
      return undefined;
    });
    const { container } = renderAt("/connections/providers", <Providers />);
    const card = await screen.findByRole("article", { name: "anthropic" });
    expect(within(card).getByText("Stored · write-only")).toBeInTheDocument();
    expect(document.body.innerHTML).not.toContain(SECRET);
    expect(screen.getByRole("link", { name: "Providers" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "Ledger" })).not.toHaveAttribute("aria-current");
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);

    const results = await axe.run(container);
    expect(results.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.html).join(" | ")}`)).toEqual([]);

    await userEvent.click(within(card).getByRole("button", { name: "Edit anthropic" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("group", { name: "API key" })).toHaveTextContent("Stored · write-only");
    expect(dialog.querySelector('input[type="password"]')).toBeNull(); // no input until Replace
    await userEvent.click(within(dialog).getByRole("button", { name: "Replace" }));
    const keyInput = within(dialog).getByLabelText("API key");
    expect(keyInput).toHaveAttribute("type", "password");
    expect(keyInput).toHaveValue("");
    expect(document.body.innerHTML).not.toContain(SECRET);

    // Saving without typing a new key must not send api_key (keeps the stored one).
    await userEvent.click(within(dialog).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/v1/providers/p1", expect.objectContaining({ method: "PATCH" })));
    const patch = fetchMock.mock.calls.find(([u, i]) => u === "/api/v1/providers/p1" && i?.method === "PATCH");
    expect(JSON.parse(String(patch?.[1]?.body))).not.toHaveProperty("api_key");
  });

  it("Test connection reports latency, model and the reply", async () => {
    mockFetch((url) => {
      if (url === "/api/v1/providers/kinds") return json(KINDS);
      if (url === "/api/v1/providers") return json([PROVIDER]);
      if (url === "/api/v1/llm/complete") return json({ provider: "anthropic", model: "claude-sonnet-4-5", content: "OK" });
      return undefined;
    });
    renderAt("/connections/providers", <Providers />);
    await userEvent.click(await screen.findByRole("button", { name: "Test connection" }));
    expect(await screen.findByText(/^Connected · \d+ ms$/)).toBeInTheDocument();
    expect(screen.getByText("“OK”")).toBeInTheDocument();
  });

  it("tests a non-Anthropic provider without a default model, and any model typed in", async () => {
    const openai = { ...PROVIDER, id: "p2", name: "openai", kind: "openai", default_model: null, api_key: undefined };
    const bodies: unknown[] = [];
    mockFetch((url, init) => {
      if (url === "/api/v1/providers/kinds") return json({ ...KINDS, kinds: [...KINDS.kinds, { ...KINDS.kinds[1], key: "openai", label: "OpenAI", self_hosted: false }] });
      if (url === "/api/v1/providers") return json([openai]);
      if (url === "/api/v1/providers/openai/models") return json({ provider: "openai", models: ["gpt-a", "gpt-b"] });
      if (url === "/api/v1/providers/openai/test") {
        bodies.push(JSON.parse(String(init.body)));
        return json({ provider: "openai", ok: true, status: "ok", detail: "2 models available", latency_ms: 12, probe: "list_models", model: null, persisted: true });
      }
      return undefined;
    });
    renderAt("/connections/providers", <Providers />);
    const card = await screen.findByRole("article", { name: "openai" });
    const button = within(card).getByRole("button", { name: "Test connection" });
    expect(button).toBeEnabled();
    await userEvent.click(button);
    expect(await within(card).findByText("2 models available")).toBeInTheDocument();
    await userEvent.type(within(card).getByLabelText("Model to test for openai"), "gpt-b");
    await userEvent.click(button);
    await waitFor(() => expect(bodies).toEqual([{}, { model: "gpt-b" }]));
  });
});

const SERVER: McpServerInfo = {
  id: "m1", name: "github", transport: "http", url: "https://mcp.example.com/mcp", command: [], header_names: ["Authorization"],
  tool_allow: ["*"], risk: ["network"], enabled: true, tools: [{ name: "create_issue", description: "Open an issue" }],
  created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z",
};

describe("McpServers", () => {
  it("confirms delete naming the server, shows only header names, and shows the Claude Code snippet", async () => {
    const fetchMock = mockFetch((url, init) => {
      if (url === "/api/v1/mcp-servers" && !init.method) return json({ servers: [SERVER], stdio_enabled: false });
      if (url === "/api/v1/mcp-servers/m1" && init.method === "DELETE") return json(null, 204);
      return undefined;
    });
    renderAt("/connections/mcp", <McpServers />);
    const card = await screen.findByRole("article", { name: "github" });
    expect(within(card).getByText("create_issue")).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Claude Code command" }).textContent).toMatch(/claude mcp add --transport http argus .*\/mcp/);
    expect(screen.getByRole("link", { name: "Settings › API keys" })).toHaveAttribute("href", "/settings/api-keys");

    await userEvent.click(within(card).getByRole("button", { name: "Delete github" }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("Delete MCP server “github”?");
    expect(fetchMock).not.toHaveBeenCalledWith("/api/v1/mcp-servers/m1", expect.objectContaining({ method: "DELETE" }));
    await userEvent.click(within(dialog).getByRole("button", { name: "Delete github" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/v1/mcp-servers/m1", expect.objectContaining({ method: "DELETE" })));
  });

  it("sends header values on create but never echoes them", async () => {
    const fetchMock = mockFetch((url, init) => {
      if (url === "/api/v1/mcp-servers" && !init.method) return json({ servers: [], stdio_enabled: false });
      if (url === "/api/v1/mcp-servers" && init.method === "POST") return json({ ...SERVER, name: "docs" }, 201);
      return undefined;
    });
    renderAt("/connections/mcp", <McpServers />);
    await userEvent.click((await screen.findAllByRole("button", { name: "Add server" }))[0]!);
    const dialog = await screen.findByRole("dialog");
    await userEvent.type(within(dialog).getByLabelText("Name"), "docs");
    await userEvent.type(within(dialog).getByLabelText("Server URL"), "https://docs.example.com/mcp");
    const value = within(dialog).getByLabelText("Headers 1 value");
    expect(value).toHaveAttribute("type", "password");
    await userEvent.type(value, "Bearer t0ken");
    await userEvent.click(within(dialog).getByRole("button", { name: "Add server" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/v1/mcp-servers", expect.objectContaining({ method: "POST" })));
    const post = fetchMock.mock.calls.find(([u, i]) => u === "/api/v1/mcp-servers" && i?.method === "POST");
    expect(JSON.parse(String(post?.[1]?.body))).toMatchObject({ name: "docs", transport: "http", headers: { Authorization: "Bearer t0ken" } });
  });
});

describe("RemoteAgents", () => {
  it("requires an agent name for ACP and shows Argus's own endpoints", async () => {
    mockFetch((url) => (url === "/api/v1/remote-agents" ? json({ agents: [] }) : undefined));
    renderAt("/connections/agents", <RemoteAgents />);
    expect(await screen.findByText("No remote agents yet")).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "A2A agent card URL" })).toHaveTextContent("/.well-known/agent-card.json");
    expect(screen.getByRole("region", { name: "ACP base URL" })).toHaveTextContent(/\/acp$/);

    await userEvent.click(screen.getAllByRole("button", { name: "Add agent" })[0]!);
    const dialog = await screen.findByRole("dialog");
    await userEvent.click(within(dialog).getByRole("radio", { name: "ACP" }));
    await userEvent.type(within(dialog).getByLabelText("Name"), "partner");
    await userEvent.type(within(dialog).getByLabelText("ACP base URL"), "https://acp.example.com/acp");
    await userEvent.click(within(dialog).getByRole("button", { name: "Add agent" }));
    expect(within(dialog).getAllByText(/ACP needs the agent's name/).length).toBeGreaterThan(0);
  });
});

const entry = (seq: number, over: Partial<LedgerEntry> = {}): LedgerEntry => ({
  seq, ts: "2026-01-01T00:00:00Z", protocol: "mcp", direction: "outbound", peer: "github", method: "tools/call", status: "ok",
  latency_ms: 42, run_id: "11111111-2222-3333-4444-555555555555", request_sha256: "a".repeat(64), response_sha256: "b".repeat(64),
  hash: `${seq}`.repeat(64).slice(0, 64), prev_hash: "0".repeat(64), detail: {}, ...over,
});

describe("Ledger", () => {
  it("lists entries with truncated hashes, expands full hashes, filters by protocol and reports a broken chain", async () => {
    const fetchMock = mockFetch((url) => {
      if (url.startsWith("/api/v1/ledger/verify")) return json({ ok: false, entries: 2, broken_at: 2, reason: "hash mismatch" });
      if (url.startsWith("/api/v1/ledger")) return json({ entries: [entry(2, { direction: "inbound", status: "error" }), entry(1)] });
      return undefined;
    });
    renderAt("/connections/ledger", <Ledger />);
    const table = await screen.findByRole("table");
    expect(within(table).getByText(`${"2".repeat(10)}…`)).toBeInTheDocument();
    expect(within(table).queryByText("2".repeat(64))).not.toBeInTheDocument();
    expect(within(table).getAllByRole("link", { name: "11111111" })[0]).toHaveAttribute("href", "/runs/11111111-2222-3333-4444-555555555555");

    await userEvent.click(screen.getByRole("button", { name: "Show hashes for entry 2" }));
    expect(within(table).getAllByText("2".repeat(64)).length).toBeGreaterThan(0);

    await userEvent.click(screen.getByRole("radio", { name: "A2A" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/v1/ledger?limit=100&protocol=a2a", expect.anything()));

    await userEvent.click(screen.getByRole("button", { name: "Verify integrity" }));
    expect(await screen.findByText("Chain broken at entry #2")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("hash mismatch");
  });
});
