// Data & privacy: account export / erasure and admin retention (backend/argus/api/v1/data.py).

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, request, V1 } from "@/lib/api";

export interface RetentionPolicy {
  run_days: number;
  event_days: number;
  audit_days: number;
  ledger_days: number;
  batch_size: number;
  interval_s: number;
  notes: string[];
}

/** Counts removed (or that would be removed, with `dry_run`) by one retention pass. */
export interface RetentionStats {
  dry_run: boolean;
  started_at: string;
  finished_at: string | null;
  duration_ms: number;
  runs: number;
  run_events: number;
  audit_events: number;
  ledger_entries: number;
  blob_dirs: number;
  blob_bytes: number;
  error?: string | null;
}

export interface RetentionInfo {
  policy: RetentionPolicy;
  last_run: RetentionStats | null;
  enabled: boolean;
}

export const dataKeys = { retention: ["admin", "retention"] as const };

export const useRetention = () =>
  useQuery({ queryKey: dataKeys.retention, queryFn: () => api.get<RetentionInfo>(`${V1}/admin/retention`) });

export function useRunRetention() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (dryRun: boolean) => api.post<RetentionStats>(`${V1}/admin/retention/run`, { dry_run: dryRun }),
    onSuccess: (_s, dryRun) => {
      if (!dryRun) void qc.invalidateQueries({ queryKey: dataKeys.retention });
    },
  });
}

/** `Content-Disposition: attachment; filename="x.zip"` → `x.zip`, or the fallback. */
export function filenameFrom(disposition: string | null, fallback: string): string {
  const m = disposition?.match(/filename\*?=(?:UTF-8'')?"?([^";]+)"?/i);
  return m?.[1] ? decodeURIComponent(m[1]) : fallback;
}

function saveBlob(blob: Blob, name: string) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/**
 * Build and download the account export. Fetched with the bearer token (an anchor link cannot carry it) and
 * saved through an object URL.
 */
export function useExportData() {
  return useMutation({
    mutationFn: async () => {
      const resp = await request<Response>(`${V1}/me/export`, { method: "POST", raw: true });
      const blob = await resp.blob();
      const name = filenameFrom(resp.headers.get("content-disposition"), "argus-export.zip");
      saveBlob(blob, name);
      return { name, size: blob.size };
    },
  });
}

export function useDeleteAccount() {
  return useMutation({
    mutationFn: (password: string) => request<void>(`${V1}/me`, { method: "DELETE", json: { password } }),
  });
}

/** Retention period in words: `0` means the data is never removed. */
export function describeDays(days: number, action: string): string {
  if (days <= 0) return "Kept forever";
  return `${action} after ${days} ${days === 1 ? "day" : "days"}`;
}

const UNITS = ["B", "KiB", "MiB", "GiB", "TiB"];

export function formatBytes(n: number): string {
  if (!Number.isFinite(n) || n < 0) return "0 B";
  let i = 0;
  let v = n;
  while (v >= 1024 && i < UNITS.length - 1) {
    v /= 1024;
    i += 1;
  }
  return i === 0 ? `${v} B` : `${v.toFixed(1)} ${UNITS[i]}`;
}
