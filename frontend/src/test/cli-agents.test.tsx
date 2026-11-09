import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import axe from "axe-core";
import { useState, type ReactNode } from "react";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import { FallbackChain, type Route } from "@/components/councils/FallbackChain";
import { ModelPicker } from "@/components/councils/ModelPicker";
import type { CliAgentInfo, CliAgentsOverview, CliLoginState } from "@/lib/types";
import CliAgents from "@/routes/connections/CliAgents";

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

const NOTICE = "Each vendor's terms govern how its CLI may be used. Subscription logins are for the account owner's own use.";
const cli = (over: Partial<CliAgentInfo>): CliAgentInfo => ({
  cli: "claude", provider: "claude-cli", label: "Claude Code", enabled: true, version: "2.1.308 (Claude Code)", installed: true,
  logged_in: true, login_method: "claude.ai", login_verified: true, status_detail: "", has_api_key: false, key_hint: null,
  key_env: "ANTHROPIC_API_KEY", login_command: "./scripts/agent-login.sh <your-email> claude", cli_login_command: "claude auth login",
  login_flow: "paste_code", login_methods: [], suggested_models: ["sonnet", "opus"], ...over,
});
const ENABLED: CliAgentsOverview = {
  enabled: true, allow_subscription: true, runner_error: null, notice: NOTICE,
  clis: [
    cli({}),
    cli({ cli: "codex", provider: "codex-cli", label: "OpenAI Codex CLI", logged_in: false, login_method: null, has_api_key: true, login_flow: "device_code",
      key_hint: "sk-…wxyz", key_env: "CODEX_API_KEY", login_command: "./scripts/agent-login.sh <your-email> codex" }),
    cli({ cli: "gemini", provider: "gemini-cli", label: "Gemini CLI", logged_in: null, login_method: null, key_env: "GEMINI_API_KEY",
      login_command: "./scripts/agent-login.sh <your-email> gemini" }),
  ],
};

describe("Connections > CLI agents", () => {
  it("explains that the feature is off and still shows the policy notice", async () => {
    mockFetch((url) => {
      if (url === "/api/v1/cli-agents") return json({ ...ENABLED, enabled: false, clis: ENABLED.clis.map((c) => ({ ...c, enabled: false })) });
      return undefined;
    });
    renderAt("/connections/cli-agents", <CliAgents />);
    expect(await screen.findByText("CLI agents are off on this instance")).toBeInTheDocument();
    expect(screen.getByText(NOTICE)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "CLI agents" })).toHaveAttribute("aria-current", "page");
  });

  it("shows status, login steps and write-only keys per CLI, and saves a key to the user's own endpoint", async () => {
    const fetchMock = mockFetch((url, init) => {
      if (url === "/api/v1/cli-agents") return json(ENABLED);
      if (url.startsWith("/api/v1/cli-agents/sessions")) return json([]);
      if (url === "/api/v1/cli-agents/gemini/key" && init.method === "PUT") return json({ cli: "gemini", has_api_key: true, key_hint: "AIz…1234" });
      return undefined;
    });
    const { container } = renderAt("/connections/cli-agents", <main><CliAgents /></main>);
    const claude = await screen.findByRole("article", { name: "Claude Code" });
    expect(within(claude).getByText("Signed in (claude.ai)")).toBeInTheDocument();
    expect(within(claude).getByRole("region", { name: "Claude Code sign-in command" })).toHaveTextContent(
      "./scripts/agent-login.sh you@example.com claude");
    const codex = screen.getByRole("article", { name: "OpenAI Codex CLI" });
    expect(within(codex).getByText("Not signed in")).toBeInTheDocument();
    expect(within(codex).getByText(/Stored · write-only · sk-…wxyz/)).toBeInTheDocument();

    const gemini = screen.getByRole("article", { name: "Gemini CLI" });
    await userEvent.type(within(gemini).getByLabelText("GEMINI_API_KEY for this CLI"), "AIzaSyTestKey000001234");
    await userEvent.click(within(gemini).getByRole("button", { name: "Save key" }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([u, i]) => String(u) === "/api/v1/cli-agents/gemini/key" && i?.method === "PUT")).toBe(true));
    const put = fetchMock.mock.calls.find(([u, i]) => String(u) === "/api/v1/cli-agents/gemini/key" && i?.method === "PUT");
    expect(JSON.parse(String(put?.[1]?.body))).toEqual({ api_key: "AIzaSyTestKey000001234" });
    const results = await axe.run(container, { rules: { "color-contrast": { enabled: false } } });
    expect(results.violations.map((v) => v.id)).toEqual([]);
  });
});

const loginState = (over: Partial<CliLoginState>): CliLoginState => ({
  cli: "claude", state: "none", flow: "paste_code", method: null, verification_url: null, user_code: null, needs_code: false,
  retry: false, expires_at: null, error: null, message: "", ...over,
});

describe("Connections > CLI agents: harness CLI keys", () => {
  it("asks which provider a Goose key is for and sends it", async () => {
    const goose = cli({ cli: "goose", provider: "goose-cli", label: "Goose", logged_in: null, login_method: null, login_flow: null,
      key_env: "ANTHROPIC_API_KEY / OPENAI_API_KEY", key_providers: ["anthropic", "openai", "openrouter"], key_provider: null });
    const fetchMock = mockFetch((url, init) => {
      if (url === "/api/v1/cli-agents") return json({ ...ENABLED, clis: [goose] });
      if (url.startsWith("/api/v1/cli-agents/sessions")) return json([]);
      if (url === "/api/v1/cli-agents/goose/key" && init.method === "PUT") return json({ cli: "goose", has_api_key: true, key_hint: "sk-…abcd", provider: "openrouter" });
      return undefined;
    });
    renderAt("/connections/cli-agents", <CliAgents />);
    const card = await screen.findByRole("article", { name: "Goose" });
    expect(within(card).queryByRole("button", { name: /Sign in/ })).toBeNull();
    await userEvent.selectOptions(within(card).getByLabelText("Key is for"), "openrouter");
    await userEvent.type(within(card).getByLabelText("API key for this CLI"), "sk-or-v1-abcdabcd");
    await userEvent.click(within(card).getByRole("button", { name: "Save key" }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([u, i]) => String(u) === "/api/v1/cli-agents/goose/key" && i?.method === "PUT")).toBe(true));
    const put = fetchMock.mock.calls.find(([u, i]) => String(u) === "/api/v1/cli-agents/goose/key" && i?.method === "PUT");
    expect(JSON.parse(String(put?.[1]?.body))).toEqual({ api_key: "sk-or-v1-abcdabcd", provider: "openrouter" });
  });
});

describe("Connections > CLI agents: in-app sign-in", () => {
  it("walks a paste-code sign-in and passes the code only to the user's own endpoint", async () => {
    let claude = loginState({});
    const fetchMock = mockFetch((url, init) => {
      if (url === "/api/v1/cli-agents") return json({ ...ENABLED, clis: [cli({ logged_in: false, login_method: null })] });
      if (url.startsWith("/api/v1/cli-agents/sessions")) return json([]);
      if (url === "/api/v1/cli-agents/claude/login" && init.method === "POST") {
        claude = loginState({ state: "waiting", needs_code: true, verification_url: "https://claude.com/cai/oauth/authorize?code=true", expires_at: 1_900_000_000 });
        return json(claude);
      }
      if (url === "/api/v1/cli-agents/claude/login/code") {
        claude = loginState({ state: "succeeded" });
        return json(loginState({ state: "verifying" }));
      }
      if (url === "/api/v1/cli-agents/claude/login") return json(claude);
      return undefined;
    });
    const { container } = renderAt("/connections/cli-agents", <main><CliAgents /></main>);
    const card = await screen.findByRole("article", { name: "Claude Code" });
    await userEvent.click(within(card).getByRole("button", { name: "Sign in with your Claude account" }));
    const panel = await within(card).findByRole("group", { name: "Claude Code sign-in" });
    const link = within(panel).getByRole("link", { name: /Open claude\.com/ });
    expect(link).toHaveAttribute("href", "https://claude.com/cai/oauth/authorize?code=true");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
    expect((await axe.run(container, { rules: { "color-contrast": { enabled: false } } })).violations.map((v) => v.id)).toEqual([]);
    const field = within(panel).getByLabelText("Code shown after you sign in");
    await userEvent.type(field, "abc123#state");
    await userEvent.click(within(panel).getByRole("button", { name: "Finish sign-in" }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([u]) => String(u) === "/api/v1/cli-agents/claude/login/code")).toBe(true));
    const sent = fetchMock.mock.calls.find(([u]) => String(u) === "/api/v1/cli-agents/claude/login/code");
    expect(JSON.parse(String(sent?.[1]?.body))).toEqual({ code: "abc123#state" });
    expect(await within(card).findByText("Claude Code is signed in", {}, { timeout: 4000 })).toBeInTheDocument();
  });

  it("shows a device code, cancels, and reports failures and expiry", async () => {
    let codex = loginState({ cli: "codex", flow: "device_code" });
    const fetchMock = mockFetch((url, init) => {
      if (url === "/api/v1/cli-agents") return json(ENABLED);
      if (url.startsWith("/api/v1/cli-agents/sessions")) return json([]);
      if (url === "/api/v1/cli-agents/codex/login" && init.method === "POST") {
        codex = loginState({ cli: "codex", flow: "device_code", state: "waiting", verification_url: "https://auth.openai.com/codex/device", user_code: "8Y19-DW049" });
        return json(codex);
      }
      if (url === "/api/v1/cli-agents/codex/login" && init.method === "DELETE") {
        codex = loginState({ cli: "codex", flow: "device_code", state: "expired", error: "timed_out", message: "The sign-in was not completed in time." });
        return json(codex);
      }
      if (url.endsWith("/login")) return json(url.includes("codex") ? codex : loginState({}));
      return undefined;
    });
    renderAt("/connections/cli-agents", <CliAgents />);
    const card = await screen.findByRole("article", { name: "OpenAI Codex CLI" });
    await userEvent.click(within(card).getByRole("button", { name: "Sign in with your ChatGPT account" }));
    const panel = await within(card).findByRole("group", { name: "OpenAI Codex CLI sign-in" });
    expect(within(panel).getByRole("region", { name: "One-time code" })).toHaveTextContent("8Y19-DW049");
    expect(within(panel).queryByLabelText("Code shown after you sign in")).toBeNull();
    await userEvent.click(within(panel).getByRole("button", { name: "Cancel" }));
    expect(await within(card).findByText("The sign-in timed out")).toBeInTheDocument();
    expect(within(card).getByText("The sign-in was not completed in time.")).toBeInTheDocument();
    expect(within(card).getByRole("button", { name: "Sign in with your ChatGPT account" })).toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([u, i]) => String(u) === "/api/v1/cli-agents/codex/login" && i?.method === "DELETE")).toBe(true);
  });

  it("signs out after confirming, and hides sign-in when only API keys are allowed", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const fetchMock = mockFetch((url) => {
      if (url === "/api/v1/cli-agents") return json(ENABLED);
      if (url.startsWith("/api/v1/cli-agents/sessions")) return json([]);
      if (url === "/api/v1/cli-agents/claude/logout") return json({ ok: true, logged_in: false, login_method: null });
      if (url.endsWith("/login")) return json(loginState({}));
      return undefined;
    });
    const view = renderAt("/connections/cli-agents", <CliAgents />);
    const card = await screen.findByRole("article", { name: "Claude Code" });
    await userEvent.click(within(card).getByRole("button", { name: "Sign out" }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([u]) => String(u) === "/api/v1/cli-agents/claude/logout")).toBe(true));
    view.unmount();

    vi.restoreAllMocks();
    mockFetch((url) => {
      if (url === "/api/v1/cli-agents") return json({ ...ENABLED, allow_subscription: false });
      if (url.startsWith("/api/v1/cli-agents/sessions")) return json([]);
      return undefined;
    });
    renderAt("/connections/cli-agents", <CliAgents />);
    const claude = await screen.findByRole("article", { name: "Claude Code" });
    expect(within(claude).queryByRole("button", { name: /Sign in/ })).toBeNull();
    expect(screen.getByText("API keys only")).toBeInTheDocument();
  });
});

const KINDS = {
  kinds: [{ key: "anthropic", label: "Anthropic Claude", protocol: "anthropic", default_base_url: null, requires_api_key: true, self_hosted: false, suggested_models: ["claude-opus-5-5"] }],
  system_providers: ["anthropic", "mock"],
  cli_providers: ["claude-cli", "codex-cli"],
};

describe("Council builder: CLI agent seats", () => {
  it("groups providers by auth type and badges an account seat", async () => {
    mockFetch((url) => {
      if (url === "/api/v1/providers/kinds") return json(KINDS);
      if (url === "/api/v1/providers") return json([]);
      return undefined;
    });
    function Harness() {
      const [route, setRoute] = useState({ provider: "anthropic", model: "claude-opus-5-5" });
      return <ModelPicker provider={route.provider} model={route.model} onChange={setRoute} />;
    }
    renderAt("/councils/new", <Harness />);
    const select = await screen.findByLabelText("Provider");
    await waitFor(() => expect(within(select).getByRole("group", { name: "Account (CLI agents)" })).toBeInTheDocument());
    expect(within(select).getByRole("group", { name: "API key" })).toBeInTheDocument();
    expect(screen.getByTestId("auth-badge")).toHaveTextContent("API key");
    await userEvent.selectOptions(select, "claude-cli");
    expect(screen.getByTestId("auth-badge")).toHaveTextContent("Account");
    expect(screen.getByLabelText("Model")).toHaveValue("sonnet");
  });

  it("edits an ordered fallback chain", async () => {
    mockFetch((url) => {
      if (url === "/api/v1/providers/kinds") return json(KINDS);
      if (url === "/api/v1/providers") return json([]);
      return undefined;
    });
    const seen: Route[][] = [];
    function Harness() {
      const [chain, setChain] = useState<Route[]>([]);
      return <FallbackChain value={chain} primary="claude-cli" onChange={(next) => { seen.push(next); setChain(next); }} />;
    }
    renderAt("/councils/new", <Harness />);
    await screen.findByRole("group", { name: "Fallback chain" });
    await waitFor(() => expect(screen.getByRole("button", { name: "Add fallback" })).toBeEnabled());
    await userEvent.click(screen.getByRole("button", { name: "Add fallback" }));
    // From an account seat the first suggestion is an API-key route.
    expect(seen.at(-1)?.[0]?.provider).toBe("anthropic");
    await userEvent.click(screen.getByRole("button", { name: "Add fallback" }));
    await userEvent.selectOptions(screen.getByLabelText("Fallback 2 provider"), "mock");
    await userEvent.click(screen.getByRole("button", { name: "Move fallback 2 up" }));
    expect(seen.at(-1)?.map((r) => r.provider)).toEqual(["mock", "anthropic"]);
    await userEvent.click(screen.getByRole("button", { name: "Remove fallback 1" }));
    expect(seen.at(-1)?.map((r) => r.provider)).toEqual(["anthropic"]);
  });
});
