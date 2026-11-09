// CLI agents (vendor CLIs as account-authenticated council seats): status, per-user keys, sessions.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, V1 } from "@/lib/api";
import { keys } from "@/lib/queries";
import type { CliAgentsOverview, CliLoginState, CliSessionInfo } from "@/lib/types";

export const cliKeys = {
  overview: ["cliAgents"] as const,
  sessions: (cli?: string) => ["cliAgents", "sessions", cli ?? "all"] as const,
  login: (cli: string) => ["cliAgents", "login", cli] as const,
};

/** Sign-in states during which the page keeps polling the runner. */
export const LOGIN_ACTIVE: ReadonlySet<string> = new Set(["starting", "waiting", "verifying"]);

/** Provider names of CLI agents; seats on these run with the user's own vendor account or CLI key. */
export const CLI_PROVIDERS: Record<string, string> = Object.fromEntries(
  ["claude", "codex", "gemini", "copilot", "kiro", "qwen", "opencode", "goose", "vibe"].map((cli) => [`${cli}-cli`, cli]),
);
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
    mutationFn: ({ cli, apiKey, provider }: { cli: string; apiKey: string; provider?: string | null }) =>
      api.put<{ cli: string; has_api_key: boolean; key_hint: string | null; provider: string | null }>(
        `${V1}/cli-agents/${encodeURIComponent(cli)}/key`, provider ? { api_key: apiKey, provider } : { api_key: apiKey }),
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

// ------------------------------------------------------------------ in-app sign-in (own user only)
const loginPath = (cli: string) => `${V1}/cli-agents/${encodeURIComponent(cli)}/login`;

/** The caller's sign-in for one CLI; polls every 2 s while it is in progress. */
export const useCliLogin = (cli: string, enabled = true) =>
  useQuery({
    queryKey: cliKeys.login(cli),
    queryFn: () => api.get<CliLoginState>(loginPath(cli)),
    enabled,
    refetchInterval: (q) => (LOGIN_ACTIVE.has(q.state.data?.state ?? "none") ? 2000 : false),
  });

function useLoginMutation<V>(cli: string, fn: (v: V) => Promise<CliLoginState>) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: fn,
    onSuccess: (state) => qc.setQueryData(cliKeys.login(cli), state),
  });
}

export const useStartCliLogin = (cli: string) =>
  useLoginMutation(cli, (method: string | null) => api.post<CliLoginState>(loginPath(cli), { method }));

export const useSubmitCliLoginCode = (cli: string) =>
  useLoginMutation(cli, (code: string) => api.post<CliLoginState>(`${loginPath(cli)}/code`, { code }));

export const useCancelCliLogin = (cli: string) =>
  useLoginMutation(cli, (_: void) => api.del<CliLoginState>(loginPath(cli)));

export function useCliLogout(cli: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () =>
      api.post<{ ok: boolean; logged_in: boolean | null; login_method: string | null }>(
        `${V1}/cli-agents/${encodeURIComponent(cli)}/logout`),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: cliKeys.overview });
      void qc.invalidateQueries({ queryKey: cliKeys.login(cli) });
    },
  });
}
