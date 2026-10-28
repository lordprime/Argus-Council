// Council mutations and picker data (TanStack Query). Reads live in @/lib/queries.

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useMemo } from "react";

import { api, V1 } from "@/lib/api";
import { MOCK_PROVIDER, MOCK_PROVIDER_LABEL } from "@/lib/providers";
import { keys, useProviderKinds, useProviders } from "@/lib/queries";
import type { Council, Member, Mode } from "@/lib/types";

export interface MemberInput {
  name: string;
  role: string;
  provider: string;
  model: string;
  system_prompt: string;
  temperature: number;
  enabled: boolean;
  config: { tools?: string[]; overseer?: boolean; [k: string]: unknown };
}

export interface NewCouncil {
  name: string;
  description: string;
  mode: Mode;
  min_size: number;
  max_size: number;
  guardrails: Record<string, unknown>;
  members?: MemberInput[];
  size?: number;
  default_provider: string;
  default_model: string;
}

export type CouncilPatch = Partial<Pick<Council, "name" | "description" | "mode" | "min_size" | "max_size" | "guardrails">>;
export type MemberPatch = Partial<Omit<MemberInput, "config">> & { position?: number; config?: MemberInput["config"] };

export function useCreateCouncil() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: NewCouncil) => api.post<Council>(`${V1}/councils`, body),
    onSuccess: (c) => {
      qc.setQueryData(keys.council(c.id), c);
      void qc.invalidateQueries({ queryKey: keys.councils });
    },
  });
}

/** Write a fresh council into both caches. */
function useStore() {
  const qc = useQueryClient();
  return (c: Council) => {
    qc.setQueryData(keys.council(c.id), c);
    qc.setQueryData<Council[]>(keys.councils, (list) => list?.map((x) => (x.id === c.id ? c : x)));
  };
}

export function useUpdateCouncil(id: string) {
  const store = useStore();
  return useMutation({
    mutationFn: (patch: CouncilPatch) => api.patch<Council>(`${V1}/councils/${id}`, patch),
    onSuccess: store,
  });
}

export function useDeleteCouncil() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.del(`${V1}/councils/${id}`),
    onSuccess: (_, id) => {
      qc.setQueryData<Council[]>(keys.councils, (list) => list?.filter((c) => c.id !== id));
      qc.removeQueries({ queryKey: keys.council(id) });
    },
  });
}

export function useResizeCouncil(id: string) {
  const store = useStore();
  return useMutation({
    mutationFn: (body: { size: number; provider?: string; model?: string }) => api.post<Council>(`${V1}/councils/${id}/resize`, body),
    onSuccess: store,
  });
}

/** Apply a member patch to a cached council (used optimistically for toggles and reordering). */
export function patchMembers(c: Council, memberId: string, patch: MemberPatch): Council {
  const { position, ...rest } = patch;
  let members = c.members.map((m) => (m.id === memberId ? ({ ...m, ...rest } as Member) : m));
  if (position != null) {
    const moving = members.find((m) => m.id === memberId);
    const others = members.filter((m) => m.id !== memberId);
    if (moving) others.splice(Math.min(position, others.length), 0, moving);
    members = others.map((m, i) => ({ ...m, position: i }));
  }
  return { ...c, members };
}

export function useUpdateMember(councilId: string) {
  const qc = useQueryClient();
  const key = keys.council(councilId);
  return useMutation({
    mutationFn: ({ memberId, patch }: { memberId: string; patch: MemberPatch }) =>
      api.patch<Member>(`${V1}/councils/${councilId}/members/${memberId}`, patch),
    onMutate: async ({ memberId, patch }) => {
      await qc.cancelQueries({ queryKey: key });
      const prev = qc.getQueryData<Council>(key);
      if (prev) qc.setQueryData(key, patchMembers(prev, memberId, patch));
      return { prev };
    },
    onError: (_e, _v, ctx) => {
      if (ctx?.prev) qc.setQueryData(key, ctx.prev);
    },
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: key });
      void qc.invalidateQueries({ queryKey: keys.councils });
    },
  });
}

export function useAddMember(councilId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: MemberInput) => api.post<Member>(`${V1}/councils/${councilId}/members`, body),
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: keys.council(councilId) });
      void qc.invalidateQueries({ queryKey: keys.councils });
    },
  });
}

export function useRemoveMember(councilId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (memberId: string) => api.del(`${V1}/councils/${councilId}/members/${memberId}`),
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: keys.council(councilId) });
      void qc.invalidateQueries({ queryKey: keys.councils });
    },
  });
}

// ------------------------------------------------------------------ provider / model options
export interface ProviderOption {
  value: string;
  label: string;
  models: string[];
  /** "account": a CLI agent signed in with the user's vendor account (or CLI key); "api_key": an API provider. */
  auth?: "account" | "api_key";
}

const CLI_OPTION: Record<string, { label: string; models: string[] }> = {
  "claude-cli": { label: "Claude Code CLI (account)", models: ["sonnet", "opus", "haiku", "default"] },
  "codex-cli": { label: "Codex CLI (account)", models: ["default"] },
  "gemini-cli": { label: "Gemini CLI (account)", models: ["default", "gemini-2.5-pro", "gemini-2.5-flash"] },
};

/**
 * Providers a seat can use: the user's enabled provider configs plus system providers
 * (env-configured). The offline `mock` provider (model `mock-1`) is always offered.
 */
export function useProviderOptions(): { options: ProviderOption[]; isLoading: boolean } {
  const providers = useProviders();
  const kinds = useProviderKinds();
  const options = useMemo(() => {
    const out: ProviderOption[] = [];
    const kindOf = (k: string) => kinds.data?.kinds.find((x) => x.key === k);
    for (const p of providers.data ?? []) {
      if (!p.enabled) continue;
      const suggested = kindOf(p.kind)?.suggested_models ?? [];
      const models = [...new Set([...(p.default_model ? [p.default_model] : []), ...suggested])];
      out.push({ value: p.name, label: `${p.name} (${kindOf(p.kind)?.label ?? p.kind})`, models, auth: "api_key" });
    }
    for (const sys of kinds.data?.system_providers ?? []) {
      if (out.some((o) => o.value === sys)) continue;
      const k = kindOf(sys);
      const models = k?.suggested_models.length ? k.suggested_models : sys === "mock" ? ["mock-1"] : [];
      out.push({ value: sys, label: sys === MOCK_PROVIDER ? MOCK_PROVIDER_LABEL : `${k?.label ?? sys} (system)`, models,
        auth: sys === MOCK_PROVIDER ? undefined : "api_key" });
    }
    for (const cli of kinds.data?.cli_providers ?? []) {
      const known = CLI_OPTION[cli];
      out.push({ value: cli, label: known?.label ?? `${cli} (account)`, models: known?.models ?? ["default"], auth: "account" });
    }
    if (!out.some((o) => o.value === MOCK_PROVIDER)) out.push({ value: MOCK_PROVIDER, label: MOCK_PROVIDER_LABEL, models: ["mock-1"] });
    return out;
  }, [providers.data, kinds.data]);
  return { options, isLoading: providers.isLoading || kinds.isLoading };
}
