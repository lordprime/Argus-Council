// CLI agents (vendor CLIs as account-authenticated council seats): status, per-user keys, sessions.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, V1 } from "@/lib/api";
import { keys } from "@/lib/queries";
import type { CliAgentsOverview, CliSessionInfo } from "@/lib/types";

export const cliKeys = {
  overview: ["cliAgents"] as const,
  sessions: (cli?: string) => ["cliAgents", "sessions", cli ?? "all"] as const,
};

/** Provider names of CLI agents; seats on these run with the user's own vendor account or CLI key. */
export const CLI_PROVIDERS: Record<string, string> = { "claude-cli": "claude", "codex-cli": "codex", "gemini-cli": "gemini" };
export const isCliProvider = (provider: string) => provider in CLI_PROVIDERS;

export const useCliAgents = () =>
  useQuery({ queryKey: cliKeys.overview, queryFn: () => api.get<CliAgentsOverview>(`${V1}/cli-agents`), staleTime: 30_000 });

export const useCliSessions = (cli?: string, enabled = true) =>
  useQuery({
    queryKey: cliKeys.sessions(cli),
    queryFn: () => api.get<CliSessionInfo[]>(`${V1}/cli-agents/sessions${cli ? `?cli=${encodeURIComponent(cli)}` : ""}`),
    enabled,
  });

export function useSetCliKey() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ cli, apiKey }: { cli: string; apiKey: string }) =>
      api.put<{ cli: string; has_api_key: boolean; key_hint: string | null }>(`${V1}/cli-agents/${encodeURIComponent(cli)}/key`, { api_key: apiKey }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: cliKeys.overview }),
  });
}

export function useDeleteCliKey() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (cli: string) => api.del(`${V1}/cli-agents/${encodeURIComponent(cli)}/key`),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: cliKeys.overview });
      void qc.invalidateQueries({ queryKey: keys.providerKinds });
    },
  });
}
