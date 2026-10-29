// Pure helpers for the insights dashboard: bucketing, formatting, exports.

import type { Run } from "@/lib/types";

import type { AuditEvent } from "./queries";

export const RANGES = [
  { value: "1", label: "1D" },
  { value: "7", label: "7D" },
  { value: "30", label: "30D" },
  { value: "90", label: "90D" },
] as const;
export type RangeValue = (typeof RANGES)[number]["value"];

export interface Bucket {
  start: number;
  label: string;
  runs: number;
  completed: number;
  failed: number;
  usd: number;
  tokens: number;
  runtime: number;
  runtimeN: number;
}

const HOUR = 3_600_000;
const DAY = 24 * HOUR;

/** Buckets runs created within the last `days` days: hourly for one day, daily otherwise. */
export function bucketRuns(runs: Run[], days: number, now = Date.now()): Bucket[] {
  const step = days <= 1 ? HOUR : DAY;
  const n = days <= 1 ? 24 : days;
  const end = Math.floor(now / step) * step + step;
  const start = end - n * step;
  const out: Bucket[] = Array.from({ length: n }, (_, i) => {
    const s = start + i * step;
    const d = new Date(s);
    return {
      start: s,
      label: step === HOUR ? `${String(d.getHours()).padStart(2, "0")}:00` : d.toLocaleDateString(undefined, { month: "short", day: "numeric" }),
      runs: 0, completed: 0, failed: 0, usd: 0, tokens: 0, runtime: 0, runtimeN: 0,
    };
  });
  for (const r of runs) {
    const t = Date.parse(r.created_at);
    if (!Number.isFinite(t) || t < start || t >= end) continue;
    const b = out[Math.floor((t - start) / step)];
    if (!b) continue;
    b.runs += 1;
    if (r.status === "completed") b.completed += 1;
    if (r.status === "failed" || r.status === "halted") b.failed += 1;
    b.usd += Number(r.usage?.usd ?? 0);
    b.tokens += Number(r.usage?.tokens_in ?? 0) + Number(r.usage?.tokens_out ?? 0);
    if (r.usage?.runtime_s) {
      b.runtime += Number(r.usage.runtime_s);
      b.runtimeN += 1;
    }
  }
  return out;
}

export const fmtUsd = (v: number) => (v >= 100 ? `$${v.toFixed(0)}` : v >= 1 ? `$${v.toFixed(2)}` : `$${v.toFixed(v === 0 ? 2 : 4)}`);
export function fmtNum(v: number) {
  if (v >= 1_000_000) return `${(v / 1_000_000).toFixed(1)}M`;
  if (v >= 10_000) return `${Math.round(v / 1000)}k`;
  if (v >= 1000) return `${(v / 1000).toFixed(1)}k`;
  return String(Math.round(v * 10) / 10);
}
export function fmtDuration(s: number | null | undefined) {
  if (s == null) return "—";
  if (s < 60) return `${s.toFixed(1)}s`;
  const m = Math.floor(s / 60);
  return `${m}m ${Math.round(s - m * 60)}s`;
}

export const sortedEntries = (o: Record<string, number> | undefined) =>
  Object.entries(o ?? {}).sort((a, b) => b[1] - a[1]);

/** Status dot tone for an error-rate percentage. */
export function rateTone(errorRate: number): "success" | "warning" | "danger" {
  return errorRate >= 20 ? "danger" : errorRate >= 5 ? "warning" : "success";
}

export type Severity = "critical" | "high" | "medium";
export const SEVERITIES: Severity[] = ["critical", "high", "medium"];
/** Guardrail rules carry no severity on the wire; classify by the rule's name. */
export function ruleSeverity(rule: string): Severity {
  // Flagged instruction-like text in tool output is only ever a warning (the text stays data).
  if (rule === "content.injection") return "medium";
  if (/secret|pii|inject|exfil|policy|forbid|deny|egress|credential/i.test(rule)) return "critical";
  if (/budget|cost|usd|token|step|time|rate|loop/i.test(rule)) return "high";
  return "medium";
}

const csvCell = (v: unknown) => {
  const s = v == null ? "" : typeof v === "string" ? v : JSON.stringify(v);
  return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
};

export function auditCsv(events: AuditEvent[]) {
  const cols: (keyof AuditEvent)[] = ["id", "ts", "actor_id", "action", "target_type", "target_id", "success", "ip", "user_agent", "detail"];
  return [cols.join(","), ...events.map((e) => cols.map((c) => csvCell(e[c])).join(","))].join("\r\n");
}

/** Saves text as a file through a short-lived Blob URL (CSP-safe: no data: navigation). */
export function downloadText(name: string, text: string, type: string) {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}
