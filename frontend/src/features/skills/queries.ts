// Skill mutations (ui-spec §3.7). Read hooks live in @/lib/queries (useSkills, useSkill).

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, request, V1 } from "@/lib/api";
import { keys } from "@/lib/queries";
import type { SkillDetail, SkillSummary } from "@/lib/types";

export interface SkillPayload {
  skill_md: string;
  files: Record<string, string>;
  enabled?: boolean;
}

export interface ValidateResult {
  valid: boolean;
  errors: string[];
  skill?: SkillSummary;
}

const enc = encodeURIComponent;

/** Like useSkill, but idle when there is no name (the editor's "new skill" mode). Shares the same cache key. */
export const useOptionalSkill = (name: string | null) =>
  useQuery({
    queryKey: keys.skill(name ?? ""),
    queryFn: () => api.get<SkillDetail>(`${V1}/skills/${enc(name ?? "")}`),
    enabled: Boolean(name),
  });

export function useValidateSkill() {
  return useMutation({
    mutationFn: (body: SkillPayload) => api.post<ValidateResult>(`${V1}/skills/validate`, body),
  });
}

export function useSaveSkill() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ name, body }: { name?: string; body: SkillPayload }) =>
      name
        ? api.put<SkillSummary>(`${V1}/skills/${enc(name)}`, { enabled: true, ...body })
        : api.post<SkillSummary>(`${V1}/skills`, { enabled: true, ...body }),
    onSuccess: (s) => {
      void qc.invalidateQueries({ queryKey: keys.skills });
      void qc.invalidateQueries({ queryKey: keys.skill(s.name) });
    },
  });
}

export function useToggleSkill() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ name, enabled }: { name: string; enabled: boolean }) =>
      api.patch<{ name: string; enabled: boolean }>(`${V1}/skills/${enc(name)}`, { enabled }),
    onMutate: async ({ name, enabled }) => {
      await qc.cancelQueries({ queryKey: keys.skills });
      const prev = qc.getQueryData<SkillSummary[]>(keys.skills);
      qc.setQueryData<SkillSummary[]>(keys.skills, (list) => list?.map((s) => (s.name === name ? { ...s, enabled } : s)));
      return { prev };
    },
    onError: (_e, _v, ctx) => {
      if (ctx?.prev) qc.setQueryData(keys.skills, ctx.prev);
    },
    onSettled: (_d, _e, { name }) => {
      void qc.invalidateQueries({ queryKey: keys.skills });
      void qc.invalidateQueries({ queryKey: keys.skill(name) });
    },
  });
}

export function useDeleteSkill() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (name: string) => api.del(`${V1}/skills/${enc(name)}`),
    onSuccess: (_d, name) => {
      qc.removeQueries({ queryKey: keys.skill(name) });
      void qc.invalidateQueries({ queryKey: keys.skills });
    },
  });
}

export function useImportSkill() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (file: File) => {
      const form = new FormData();
      form.append("file", file);
      return request<SkillSummary>(`${V1}/skills/import`, { method: "POST", body: form });
    },
    onSuccess: () => void qc.invalidateQueries({ queryKey: keys.skills }),
  });
}

/**
 * Download the skill as a zip. `request(..., { raw })` sends the in-memory bearer token (refreshing it once on
 * 401) and maps error envelopes, so a plain anchor link (which cannot carry the header) is not used.
 */
export async function exportSkill(name: string): Promise<void> {
  const resp = await request<Response>(`${V1}/skills/${enc(name)}/export`, { raw: true });
  const blob = await resp.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `${name}.zip`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// ------------------------------------------------------------------ version history
export interface SkillVersionMeta {
  number: number;
  version: string | null;
  created_at: string;
  author: string;
  note: string;
  files: string[];
}

export interface SkillVersionDetail extends SkillVersionMeta {
  name: string;
  skill_md: string;
  file_contents: Record<string, string>;
}

export interface RestoreResult extends SkillSummary {
  restored_from: number;
  current_version: number | null;
  version_count: number;
}

export const skillKeys = {
  versions: (name: string) => ["skill", name, "versions"] as const,
  version: (name: string, n: number) => ["skill", name, "versions", n] as const,
  marketplace: ["skills", "marketplace"] as const,
};

export const useSkillVersions = (name: string, enabled = true) =>
  useQuery({
    queryKey: skillKeys.versions(name),
    queryFn: () => api.get<{ name: string; current: number | null; versions: SkillVersionMeta[] }>(`${V1}/skills/${enc(name)}/versions`),
    enabled: Boolean(name) && enabled,
  });

export const useSkillVersion = (name: string, n: number | null) =>
  useQuery({
    queryKey: skillKeys.version(name, n ?? 0),
    queryFn: () => api.get<SkillVersionDetail>(`${V1}/skills/${enc(name)}/versions/${n}`),
    enabled: Boolean(name) && n !== null,
    staleTime: Infinity, // versions are immutable
  });

export function useRestoreVersion() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ name, n }: { name: string; n: number }) => api.post<RestoreResult>(`${V1}/skills/${enc(name)}/versions/${n}/restore`),
    onSuccess: async (_r, { name }) => {
      void qc.invalidateQueries({ queryKey: keys.skills });
      await qc.invalidateQueries({ queryKey: keys.skill(name) });
    },
  });
}

// ------------------------------------------------------------------ marketplace
export interface MarketplaceSkill {
  name: string;
  description: string;
  version: string;
  tags: string[];
  allowed_tools: string[];
  files: string[];
  installed: boolean;
  installed_version: string | null;
  installed_from_marketplace: boolean;
}

export const useMarketplace = (enabled = true) =>
  useQuery({
    queryKey: skillKeys.marketplace,
    queryFn: () => api.get<{ skills: MarketplaceSkill[] }>(`${V1}/skills/marketplace`).then((r) => r.skills),
    enabled,
  });

export function useInstallSkill() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (name: string) => api.post<SkillSummary>(`${V1}/skills/marketplace/${enc(name)}/install`),
    onSuccess: (s) => {
      qc.setQueryData<MarketplaceSkill[]>(skillKeys.marketplace, (list) =>
        list?.map((m) => (m.name === s.name ? { ...m, installed: true, installed_version: s.version, installed_from_marketplace: true } : m)));
      void qc.invalidateQueries({ queryKey: skillKeys.marketplace });
      void qc.invalidateQueries({ queryKey: keys.skills });
    },
  });
}
