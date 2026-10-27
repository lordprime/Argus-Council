// Settings-area hooks: profile (GET/PATCH /auth/me) and API keys (create / revoke).

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, V1 } from "@/lib/api";
import { keys } from "@/lib/queries";
import type { ApiKeyInfo, User } from "@/lib/types";
import { useSession } from "@/stores/session";

export const meKey = ["me"] as const;

/** Current user. Seeds from the session store so the page renders instantly, then refreshes. */
export function useMe() {
  const cached = useSession.getState().user;
  return useQuery({
    queryKey: meKey,
    queryFn: () => api.get<User>(`${V1}/auth/me`),
    initialData: cached ?? undefined,
    initialDataUpdatedAt: 0,
  });
}

export interface ProfilePatch {
  display_name?: string;
  current_password?: string;
  new_password?: string;
}

export function useUpdateMe() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: ProfilePatch) => api.patch<User>(`${V1}/auth/me`, body),
    onSuccess: (user) => {
      qc.setQueryData(meKey, user);
      // Keep the shell (user menu, avatar initials) in sync without a reload.
      useSession.setState({ user });
    },
  });
}

export interface ApiKeyCreated extends ApiKeyInfo {
  /** Plaintext key. The server returns it exactly once. */
  key: string;
}

export interface NewApiKey {
  name: string;
  scopes: string[];
  expires_in_days?: number;
}

export function useCreateApiKey() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: NewApiKey) => api.post<ApiKeyCreated>(`${V1}/auth/api-keys`, body),
    onSuccess: () => qc.invalidateQueries({ queryKey: keys.apiKeys }),
  });
}

export function useRevokeApiKey() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.del(`${V1}/auth/api-keys/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: keys.apiKeys }),
  });
}
