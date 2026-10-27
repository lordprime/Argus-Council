// Insights + audit reads (GET /api/v1/insights, /api/v1/audit). Runs are read separately for time series.

import { useInfiniteQuery, useQuery } from "@tanstack/react-query";

import { api, V1 } from "@/lib/api";
import type { ProviderConfig, Run } from "@/lib/types";

export interface InsightsTotals {
  runs: number;
  cost_usd: number;
  tokens_in: number;
  tokens_out: number;
  avg_runtime_s: number | null;
  success_rate: number;
}
export interface ModelStat { model: string; calls: number; cost_usd: number; tokens: number; avg_latency_ms: number }
export interface ToolStat { tool: string; calls: number; error_rate: number; blocked: number; avg_ms: number }
export interface Insights {
  window_days: number;
  analysed_runs: number;
  totals: InsightsTotals;
  runs_by_status: Record<string, number>;
  runs_by_mode: Record<string, number>;
  models: ModelStat[];
  tools: ToolStat[];
  oversight: {
    intercepts_by_reason: Record<string, number>;
    intercepts_by_action: Record<string, number>;
    approvals: Record<string, number>;
  };
  guardrail_violations: Record<string, number>;
  top_failures: { reason: string; count: number }[];
}

export interface AuditEvent {
  id: number;
  ts: string;
  actor_id: string | null;
  action: string;
  target_type: string | null;
  target_id: string | null;
  success: boolean;
  ip: string | null;
  user_agent: string | null;
  detail: Record<string, unknown> | null;
}

/** Provider config plus the optional health probe fields newer backends return. */
export type ProviderWithHealth = ProviderConfig & {
  health_status?: string | null;
  health_latency_ms?: number | null;
  health_checked_at?: string | null;
};

export const insightKeys = {
  insights: (days: number) => ["insights", days] as const,
  runs: ["insights", "runs"] as const,
  providers: ["providers"] as const,
  audit: (f: AuditFilters) => ["audit", f] as const,
};

export const useInsights = (days: number) =>
  useQuery({ queryKey: insightKeys.insights(days), queryFn: () => api.get<Insights>(`${V1}/insights?days=${days}`) });

/** The newest 200 runs; enough for sparklines and the runs-over-time chart. */
export const useInsightRuns = () =>
  useQuery({ queryKey: insightKeys.runs, queryFn: () => api.get<Run[]>(`${V1}/runs?limit=200`) });

export const useProviderHealth = () =>
  useQuery({ queryKey: insightKeys.providers, queryFn: () => api.get<ProviderWithHealth[]>(`${V1}/providers`) });

export interface AuditFilters {
  action: string;
  success: "" | "true" | "false";
  all: boolean;
}
export const AUDIT_PAGE = 50;

export function auditUrl(f: AuditFilters, beforeId?: number, limit = AUDIT_PAGE) {
  const qs = new URLSearchParams();
  if (f.action.trim()) qs.set("action", f.action.trim());
  if (f.success) qs.set("success", f.success);
  if (f.all) qs.set("all", "true");
  if (beforeId) qs.set("before_id", String(beforeId));
  qs.set("limit", String(limit));
  return `${V1}/audit?${qs}`;
}

export const useAuditLog = (f: AuditFilters) =>
  useInfiniteQuery({
    queryKey: insightKeys.audit(f),
    initialPageParam: undefined as number | undefined,
    queryFn: ({ pageParam }) => api.get<{ events: AuditEvent[] }>(auditUrl(f, pageParam)).then((r) => r.events),
    getNextPageParam: (last) => (last.length >= AUDIT_PAGE ? last[last.length - 1]?.id : undefined),
  });
