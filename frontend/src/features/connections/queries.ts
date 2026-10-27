// Connection mutations and extra reads (ui-spec §3.8). List hooks live in @/lib/queries.

import { useMutation, useQuery, useQueryClient, type QueryKey } from "@tanstack/react-query";

import { api, ApiError, V1 } from "@/lib/api";
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
export interface McpCreate {
  name: string;
  transport: "http" | "stdio";
  url?: string;
  command?: string[];
  headers?: Record<string, string>;
  tool_allow: string[];
  risk: string[];
  enabled: boolean;
}
/** `headers`: omit to keep, `{}` to clear, a dict to replace (values are write-only). */
export interface McpPatch {
  url?: string;
  headers?: Record<string, string>;
  tool_allow?: string[];
  risk?: string[];
  enabled?: boolean;
}

export const useCreateMcp = () => useInvalidating(keys.mcp, (body: McpCreate) => api.post<McpServerInfo>(`${V1}/mcp-servers`, body));
export const useUpdateMcp = () =>
  useInvalidating(keys.mcp, ({ id, patch }: { id: string; patch: McpPatch }) => api.patch<McpServerInfo>(`${V1}/mcp-servers/${enc(id)}`, patch));
export const useDeleteMcp = () => useInvalidating(keys.mcp, (id: string) => api.del(`${V1}/mcp-servers/${enc(id)}`));
export const useDiscoverMcp = () =>
  useInvalidating(keys.mcp, (id: string) =>
    api.post<McpServerInfo & { server_info?: { name?: string; version?: string } }>(`${V1}/mcp-servers/${enc(id)}/discover`));

// ---------------------------------------------------------------- remote agents
export interface AgentCreate {
  name: string;
  protocol: "a2a" | "acp";
  url: string;
  agent_name?: string | null;
  token?: string | null;
  description: string;
  enabled: boolean;
}
/** `token`: omit to keep, `null` to clear, a string to replace. */
export interface AgentPatch {
  url?: string;
  token?: string | null;
  description?: string;
  enabled?: boolean;
}

export const useCreateAgent = () =>
  useInvalidating(keys.agents, (body: AgentCreate) => api.post<RemoteAgentInfo>(`${V1}/remote-agents`, body));
export const useUpdateAgent = () =>
  useInvalidating(keys.agents, ({ id, patch }: { id: string; patch: AgentPatch }) =>
    api.patch<RemoteAgentInfo>(`${V1}/remote-agents/${enc(id)}`, patch));
export const useDeleteAgent = () => useInvalidating(keys.agents, (id: string) => api.del(`${V1}/remote-agents/${enc(id)}`));
export const useRefreshAgent = () =>
  useInvalidating(keys.agents, (id: string) => api.post<RemoteAgentInfo>(`${V1}/remote-agents/${enc(id)}/refresh`));

// ---------------------------------------------------------------- ledger
export const useLedgerPage = (protocol: string | undefined, limit: number) =>
  useQuery({
    queryKey: [...keys.ledger(protocol), limit],
    queryFn: () => {
      const qs = new URLSearchParams({ limit: String(limit) });
      if (protocol) qs.set("protocol", protocol);
      return api.get<{ entries: LedgerEntry[] }>(`${V1}/ledger?${qs}`).then((r) => r.entries);
    },
  });

export type VerifyResult =
  | { ok: true; entries: number; head: string }
  | { ok: false; entries: number; broken_at: number; reason: string };

export const useVerifyLedger = () => useMutation({ mutationFn: () => api.get<VerifyResult>(`${V1}/ledger/verify`) });
