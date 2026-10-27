// TanStack Query hooks and keys (ui-spec §6.4).

import { QueryClient, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, ApiError, V1 } from "./api";
import type {
  ApiKeyInfo,
  ArtifactInfo,
  Council,
  LedgerEntry,
  McpServerInfo,
  Mode,
  OutputFormat,
  ProviderConfig,
  ProviderKind,
  RemoteAgentInfo,
  RoleInfo,
  Run,
  RunDetail,
  SkillDetail,
  SkillSummary,
} from "./types";

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 15_000,
      retry: (count, err) => !(err instanceof ApiError && err.status < 500) && count < 2,
      refetchOnWindowFocus: false,
    },
  },
});

export const keys = {
  roles: ["roles"] as const,
  councils: ["councils"] as const,
  council: (id: string) => ["council", id] as const,
  runs: (filters: Record<string, string | undefined> = {}) => ["runs", filters] as const,
  run: (id: string) => ["run", id] as const,
  artifacts: (runId: string) => ["artifacts", runId] as const,
  providers: ["providers"] as const,
  providerKinds: ["providerKinds"] as const,
  skills: ["skills"] as const,
  skill: (name: string) => ["skill", name] as const,
  apiKeys: ["apiKeys"] as const,
  mcp: ["mcp"] as const,
  agents: ["agents"] as const,
  ledger: (protocol?: string) => ["ledger", protocol ?? "all"] as const,
};

export const useRoles = () =>
  useQuery({ queryKey: keys.roles, queryFn: () => api.get<{ roles: RoleInfo[]; rosters: Record<Mode, string[]> }>(`${V1}/roles`), staleTime: Infinity });

export const useCouncils = () => useQuery({ queryKey: keys.councils, queryFn: () => api.get<Council[]>(`${V1}/councils`) });
export const useCouncil = (id: string) => useQuery({ queryKey: keys.council(id), queryFn: () => api.get<Council>(`${V1}/councils/${id}`) });

export function useRunsList(filters: { status?: string; mode?: string; limit?: number } = {}) {
  const qs = new URLSearchParams(Object.entries(filters).filter(([, v]) => v != null && v !== "").map(([k, v]) => [k, String(v)]));
  return useQuery({
    queryKey: keys.runs(Object.fromEntries(qs)),
    queryFn: () => api.get<Run[]>(`${V1}/runs${qs.size ? `?${qs}` : ""}`),
    refetchInterval: 10_000,
  });
}

export const useRunDetail = (id: string) =>
  useQuery({ queryKey: keys.run(id), queryFn: () => api.get<RunDetail>(`${V1}/runs/${id}`) });

export const useArtifacts = (runId: string, enabled = true) =>
  useQuery({
    queryKey: keys.artifacts(runId),
    queryFn: () => api.get<{ artifacts: ArtifactInfo[] }>(`${V1}/runs/${runId}/artifacts`).then((r) => r.artifacts),
    enabled,
  });

export const useProviderKinds = () =>
  useQuery({
    queryKey: keys.providerKinds,
    queryFn: () => api.get<{ kinds: ProviderKind[]; system_providers: string[] }>(`${V1}/providers/kinds`),
    staleTime: 60_000,
  });
export const useProviders = () => useQuery({ queryKey: keys.providers, queryFn: () => api.get<ProviderConfig[]>(`${V1}/providers`) });

export const useSkills = () =>
  useQuery({ queryKey: keys.skills, queryFn: () => api.get<{ skills: SkillSummary[] }>(`${V1}/skills`).then((r) => r.skills) });
export const useSkill = (name: string) =>
  useQuery({ queryKey: keys.skill(name), queryFn: () => api.get<SkillDetail>(`${V1}/skills/${name}`) });

export const useApiKeys = () => useQuery({ queryKey: keys.apiKeys, queryFn: () => api.get<ApiKeyInfo[]>(`${V1}/auth/api-keys`) });

export const useMcpServers = () =>
  useQuery({ queryKey: keys.mcp, queryFn: () => api.get<{ servers: McpServerInfo[]; stdio_enabled: boolean }>(`${V1}/mcp-servers`) });
export const useRemoteAgents = () =>
  useQuery({ queryKey: keys.agents, queryFn: () => api.get<{ agents: RemoteAgentInfo[] }>(`${V1}/remote-agents`).then((r) => r.agents) });
export const useLedger = (protocol?: string) =>
  useQuery({
    queryKey: keys.ledger(protocol),
    queryFn: () => api.get<{ entries: LedgerEntry[] }>(`${V1}/ledger${protocol ? `?protocol=${protocol}` : ""}`).then((r) => r.entries),
  });

export interface NewRun {
  topic: string;
  description: string;
  mode: Mode;
  council_id?: string | null;
  size?: number;
  provider?: string;
  model?: string;
  outputs: OutputFormat[];
  guardrails: Record<string, unknown>;
}

export function useCreateRun() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: NewRun) => api.post<Run>(`${V1}/runs`, body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["runs"] }),
  });
}
