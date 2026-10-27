// Connection mutations and extra reads (ui-spec §3.8). List hooks live in @/lib/queries.

import { useMutation, useQuery, useQueryClient, type QueryKey } from "@tanstack/react-query";

import { api, ApiError, errorMessage, V1 } from "@/lib/api";
import { keys } from "@/lib/queries";
import type { LedgerEntry, McpServerInfo, ProviderConfig, RemoteAgentInfo } from "@/lib/types";

const enc = encodeURIComponent;

function useInvalidating<TVars, TData>(key: QueryKey, fn: (v: TVars) => Promise<TData>) {
  const qc = useQueryClient();
  return useMutation({ mutationFn: fn, onSuccess: () => void qc.invalidateQueries({ queryKey: key }) });
}

// ---------------------------------------------------------------- providers
export interface ProviderCreate {
  name: string;
  kind: string;
  base_url?: string | null;
  api_key?: string | null;
  default_model?: string | null;
  enabled: boolean;
  options: Record<string, unknown>;
}
/** `api_key`: omit to keep the stored key, `null` to clear it, a string to replace it. */
export interface ProviderPatch {
  base_url?: string | null;
  api_key?: string | null;
  default_model?: string | null;
  enabled?: boolean;
}

export const useCreateProvider = () =>
  useInvalidating(keys.providers, (body: ProviderCreate) => api.post<ProviderConfig>(`${V1}/providers`, body));
export const useUpdateProvider = () =>
  useInvalidating(keys.providers, ({ id, patch }: { id: string; patch: ProviderPatch }) =>
    api.patch<ProviderConfig>(`${V1}/providers/${enc(id)}`, patch));
export const useDeleteProvider = () => useInvalidating(keys.providers, (id: string) => api.del(`${V1}/providers/${enc(id)}`));

export const useProviderModels = (name: string, enabled: boolean) =>
  useQuery({
    queryKey: ["providerModels", name],
    queryFn: () => api.get<{ provider: string; models: string[] }>(`${V1}/providers/${enc(name)}/models`).then((r) => r.models),
    enabled,
    retry: false,
    staleTime: 60_000,
  });

export type HealthStatus = "unknown" | "ok" | "unreachable" | "auth_failed" | "rate_limited" | "error";

/** Fields the backend adds once provider health is persisted; all optional so older servers still render. */
export interface ProviderHealth {
  key_hint?: string | null;
  health_status?: HealthStatus | null;
  health_detail?: string | null;
  health_checked_at?: string | null;
  health_latency_ms?: number | null;
}
export type ProviderWithHealth = ProviderConfig & ProviderHealth;

export interface TestResult {
  ok: boolean;
  latencyMs: number;
  model: string;
  text: string;
  /** "server": the persisting health probe (text is a status detail); otherwise text is the model's reply. */
  source?: "server" | "completion";
}

type ServerTest = ProviderHealth & { ok?: boolean; latency_ms?: number | null; detail?: string | null; model?: string | null; content?: string; status?: HealthStatus };

/**
 * "Test connection". Prefers the persisting endpoint (POST /providers/{name}/test), which stores health on the provider,
 * and falls back to one tiny completion through the router on servers without it. Resolves whether it succeeds or not,
 * and always refetches the provider list so persisted health shows.
 */
export function useTestProvider() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({ provider, model }: { provider: string; model: string }): Promise<TestResult> => {
      const t0 = performance.now();
      const elapsed = () => Math.round(performance.now() - t0);
      try {
        const r = await api.post<ServerTest>(`${V1}/providers/${enc(provider)}/test`, model ? { model } : {});
        const ok = r.ok ?? (r.status ?? r.health_status) === "ok";
        return {
          ok,
          latencyMs: r.latency_ms ?? r.health_latency_ms ?? elapsed(),
          model: r.model || model,
          text: (r.detail ?? r.health_detail ?? r.content ?? "").slice(0, 200),
          source: "server",
        };
      } catch (err) {
        if (!(err instanceof ApiError && (err.status === 404 || err.status === 405))) {
          return { ok: false, latencyMs: elapsed(), model, text: (err instanceof Error ? err.message : "Request failed").slice(0, 200) };
        }
      }
      try {
        const r = await api.post<{ model: string; content: string }>(`${V1}/llm/complete`, {
          provider,
          model,
          messages: [{ role: "user", content: "Reply with the single word OK." }],
          max_tokens: 16,
        });
        return { ok: true, latencyMs: elapsed(), model: r.model || model, text: (r.content ?? "").slice(0, 200) };
      } catch (err) {
        const msg = err instanceof Error ? err.message : "Request failed";
        return { ok: false, latencyMs: elapsed(), model, text: msg.slice(0, 200) };
      }
    },
    onSettled: () => void qc.invalidateQueries({ queryKey: keys.providers }),
  });
}

// ---------------------------------------------------------------- MCP servers
/** trusted: tools run without extra approval · ask: every call needs approval · blocked: never attached to runs. */
export type McpTrust = "trusted" | "ask" | "blocked";
/** Server fields added with trust levels; optional so older servers still render ("ask" is the backend default). */
export type McpServer = McpServerInfo & { trust?: McpTrust; require_approval?: string[] };
export const trustOf = (s: McpServer): McpTrust => s.trust ?? "ask";

export interface McpCreate {
  name: string;
  transport: "http" | "stdio";
  url?: string;
  command?: string[];
  headers?: Record<string, string>;
  tool_allow: string[];
  risk: string[];
  trust: McpTrust;
  require_approval: string[];
  enabled: boolean;
}
/** `headers`: omit to keep, `{}` to clear, a dict to replace (values are write-only). */
export interface McpPatch {
  url?: string;
  headers?: Record<string, string>;
  tool_allow?: string[];
  risk?: string[];
  trust?: McpTrust;
  require_approval?: string[];
  enabled?: boolean;
}

export const useCreateMcp = () => useInvalidating(keys.mcp, (body: McpCreate) => api.post<McpServer>(`${V1}/mcp-servers`, body));
export const useUpdateMcp = () =>
  useInvalidating(keys.mcp, ({ id, patch }: { id: string; patch: McpPatch }) => api.patch<McpServer>(`${V1}/mcp-servers/${enc(id)}`, patch));
export const useDeleteMcp = () => useInvalidating(keys.mcp, (id: string) => api.del(`${V1}/mcp-servers/${enc(id)}`));

export type DiscoverResponse = McpServer & { server_info?: { name?: string; version?: string } };
export interface ConnectionTestResult {
  ok: boolean;
  latencyMs: number;
  /** Tools the server offered that pass tool_allow (discovery stores only those). */
  tools: McpServer["tools"];
  server?: DiscoverResponse;
  serverName?: string;
  error?: string;
}

/**
 * "Test connection": runs discovery (initialize + tools/list, both ledgered with purpose "discover") and resolves with
 * a summary whether it succeeds or not, so callers render one result shape.
 */
export function useMcpConnectionTest() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (id: string): Promise<ConnectionTestResult> => {
      const t0 = performance.now();
      try {
        const r = await api.post<DiscoverResponse>(`${V1}/mcp-servers/${enc(id)}/discover`);
        const info = r.server_info;
        return {
          ok: true, latencyMs: Math.round(performance.now() - t0), tools: r.tools ?? [], server: r,
          serverName: info?.name ? `${info.name}${info.version ? ` ${info.version}` : ""}` : undefined,
        };
      } catch (err) {
        return { ok: false, latencyMs: Math.round(performance.now() - t0), tools: [], error: errorMessage(err).slice(0, 300) };
      }
    },
    onSettled: () => void qc.invalidateQueries({ queryKey: keys.mcp }),
  });
}

/** fnmatch-style match for tool_allow patterns ("*", "search_*"). */
export function toolAllowed(tool: string, patterns: string[]): boolean {
  return patterns.some((p) => new RegExp(`^${p.replace(/[.+^${}()|[\]\\]/g, "\\$&").replace(/\*/g, ".*").replace(/\?/g, ".")}$`).test(tool));
}

/** Risk tags a tool carries in a run, mirroring the backend: "ask" (or an always-ask tool) adds the approval tag. */
export function effectiveToolRisk(s: { risk: string[]; trust: McpTrust; require_approval: string[] }, tool: string): string[] {
  return (s.trust === "ask" || s.require_approval.includes(tool)) && !s.risk.includes("approval") ? [...s.risk, "approval"] : s.risk;
}

// ---------------------------------------------------------------- remote agents
export interface AgentCreate {
  name: string;
  protocol: "a2a" | "acp";
  url: string;
  agent_name?: string | null;
  token?: string | null;
  description: string;
  enabled: boolean;
  requires_approval: boolean;
}
/** `token`: omit to keep, `null` to clear, a string to replace. */
export interface AgentPatch {
  url?: string;
  token?: string | null;
  description?: string;
  enabled?: boolean;
  requires_approval?: boolean;
  /** ACP only (the backend answers 400 for A2A). Changing it clears the cached card. */
  agent_name?: string;
}

/** Agent fields added with approval gating; optional so older servers still render (backend default true). */
export type RemoteAgent = RemoteAgentInfo & { requires_approval?: boolean };

export const useCreateAgent = () =>
  useInvalidating(keys.agents, (body: AgentCreate) => api.post<RemoteAgentInfo>(`${V1}/remote-agents`, body));
export const useUpdateAgent = () =>
  useInvalidating(keys.agents, ({ id, patch }: { id: string; patch: AgentPatch }) =>
    api.patch<RemoteAgentInfo>(`${V1}/remote-agents/${enc(id)}`, patch));
export const useDeleteAgent = () => useInvalidating(keys.agents, (id: string) => api.del(`${V1}/remote-agents/${enc(id)}`));

export interface RefreshResult {
  ok: boolean;
  latencyMs: number;
  agent?: RemoteAgent;
  error?: string;
}
/** Refresh the A2A card (`agent-card`) or confirm the ACP agent (`agents:list`); resolves with a summary either way. */
export function useRefreshAgent() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (id: string): Promise<RefreshResult> => {
      const t0 = performance.now();
      try {
        const agent = await api.post<RemoteAgent>(`${V1}/remote-agents/${enc(id)}/refresh`);
        return { ok: true, latencyMs: Math.round(performance.now() - t0), agent };
      } catch (err) {
        return { ok: false, latencyMs: Math.round(performance.now() - t0), error: errorMessage(err).slice(0, 300) };
      }
    },
    onSettled: () => void qc.invalidateQueries({ queryKey: keys.agents }),
  });
}

const str = (v: unknown): string => (typeof v === "string" ? v : typeof v === "number" ? String(v) : "");

export interface CardSummary {
  name: string;
  version: string;
  description: string;
  skills: { id: string; name: string; description: string }[];
  /** Declared capabilities that are on (A2A `capabilities`, ACP `metadata.capabilities`). */
  capabilities: string[];
  inputModes: string[];
  outputModes: string[];
}

/** Normalizes an A2A agent card or an ACP agent manifest. Remote data is untrusted: strings only, never markup. */
export function summarizeCard(card: Record<string, unknown> | undefined): CardSummary | null {
  if (!card || !Object.keys(card).length) return null;
  const obj = (v: unknown) => (v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : {});
  const strs = (v: unknown) => (Array.isArray(v) ? v.map(str).filter(Boolean) : []);
  const meta = obj(card.metadata);
  const skills = (Array.isArray(card.skills) ? card.skills : []).map((x, i) => {
    const s = obj(x);
    return { id: str(s.id) || String(i), name: str(s.name) || str(s.id), description: str(s.description) };
  });
  const capObj = obj(card.capabilities);
  const capabilities = [
    ...Object.entries(capObj).filter(([, v]) => v === true).map(([k]) => k),
    ...(Array.isArray(meta.capabilities)
      ? meta.capabilities.map((c) => str(obj(c).name) || str(c)).filter(Boolean)
      : []),
  ];
  return {
    name: str(card.name),
    version: str(card.version),
    description: str(card.description),
    skills,
    capabilities,
    inputModes: [...strs(card.defaultInputModes), ...strs(card.input_content_types)],
    outputModes: [...strs(card.defaultOutputModes), ...strs(card.output_content_types)],
  };
}

// ---------------------------------------------------------------- ledger
export type LedgerPurpose = "run" | "discover" | "refresh";

/** Why a ledger entry was made: detail.purpose when recorded, else "run" for entries tied to a run. */
export function entryPurpose(e: LedgerEntry): LedgerPurpose | null {
  const p = e.detail?.purpose;
  if (p === "run" || p === "discover" || p === "refresh") return p;
  return e.run_id ? "run" : null;
}

export const useLedgerPage = (protocol: string | undefined, limit: number, runId?: string) =>
  useQuery({
    queryKey: [...keys.ledger(protocol), limit, runId ?? ""],
    queryFn: () => {
      const qs = new URLSearchParams({ limit: String(limit) });
      if (protocol) qs.set("protocol", protocol);
      if (runId) qs.set("run_id", runId);
      return api.get<{ entries: LedgerEntry[] }>(`${V1}/ledger?${qs}`).then((r) => r.entries);
    },
  });

export type VerifyResult =
  | { ok: true; entries: number; head: string }
  | { ok: false; entries: number; broken_at: number; reason: string };

export const useVerifyLedger = () => useMutation({ mutationFn: () => api.get<VerifyResult>(`${V1}/ledger/verify`) });
