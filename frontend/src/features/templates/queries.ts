// Run templates (backend/argus/api/v1/templates.py): saved run configurations whose topic and
// description may contain {{variables}} that are filled in when the template is run.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, V1 } from "@/lib/api";
import type { Mode, OutputFormat, Run } from "@/lib/types";

export interface RunTemplate {
  id: string;
  name: string;
  summary: string;
  topic: string;
  description: string;
  mode: Mode;
  council_id: string | null;
  size: number | null;
  provider: string;
  model: string;
  outputs: OutputFormat[];
  guardrails: Record<string, unknown>;
  variables: string[];
  use_count: number;
  last_used_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface TemplateConfig {
  topic: string;
  description?: string;
  mode: Mode;
  council_id?: string | null;
  size?: number | null;
  provider?: string;
  model?: string;
  outputs: OutputFormat[];
  guardrails: Record<string, unknown>;
}

export type TemplateCreate = TemplateConfig & { name: string; summary?: string };

export const templateKeys = {
  all: ["templates"] as const,
  one: (id: string) => ["template", id] as const,
};

const BASE = `${V1}/templates`;
const enc = encodeURIComponent;

export const useTemplates = () =>
  useQuery({ queryKey: templateKeys.all, queryFn: () => api.get<{ templates: RunTemplate[] }>(BASE).then((r) => r.templates) });

function useTemplateMutation<TVars, TData>(fn: (v: TVars) => Promise<TData>, invalidateRuns = false) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: fn,
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: templateKeys.all });
      if (invalidateRuns) void qc.invalidateQueries({ queryKey: ["runs"] });
    },
  });
}

export const useCreateTemplate = () => useTemplateMutation((body: TemplateCreate) => api.post<RunTemplate>(BASE, body));
export const useUpdateTemplate = () =>
  useTemplateMutation(({ id, patch }: { id: string; patch: Partial<TemplateCreate> }) =>
    api.patch<RunTemplate>(`${BASE}/${enc(id)}`, patch));
export const useDeleteTemplate = () => useTemplateMutation((id: string) => api.del(`${BASE}/${enc(id)}`));
export const useRunTemplate = () =>
  useTemplateMutation(({ id, variables }: { id: string; variables: Record<string, string> }) =>
    api.post<Run>(`${BASE}/${enc(id)}/run`, { variables }), true);

// Mirrors backend VAR_RE: {{ name }} with an identifier name.
const VAR_RE = /\{\{\s*([A-Za-z_][A-Za-z0-9_]{0,63})\s*\}\}/g;

/** Distinct variable names in order of first appearance. */
export function templateVariables(...texts: (string | undefined)[]): string[] {
  const seen = new Set<string>();
  for (const t of texts) for (const m of (t ?? "").matchAll(VAR_RE)) seen.add(m[1]!);
  return [...seen];
}

/** Fill variables; unfilled ones are left as their {{placeholder}}. */
export function fillTemplate(text: string, values: Record<string, string>): string {
  return text.replace(VAR_RE, (whole, name: string) => (values[name]?.trim() ? values[name]!.trim() : whole));
}

/** Human label for a variable name: product_name → Product name. */
export function variableLabel(name: string): string {
  const s = name.replace(/_+/g, " ").trim();
  return s.charAt(0).toUpperCase() + s.slice(1);
}
