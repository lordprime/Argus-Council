// Shared cells for a run row (audit C-14, spec §3.2 / §3.10). Home's stacked rows, the Runs table and
// the Runs mobile list are all built from these, so a run reads the same everywhere: status, topic,
// mode, council, created, elapsed and cost with a budget microbar.

import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";

import { StatusPill } from "@/components/ui";
import { api, V1 } from "@/lib/api";
import { cn } from "@/lib/cn";
import { duration, relTime, usd } from "@/lib/format";
import { keys } from "@/lib/queries";
import type { Council, Run } from "@/lib/types";

import { ModeLabel } from "./ModeLabel";

export function runElapsed(run: Run): number | null {
  if (run.usage?.runtime_s != null) return run.usage.runtime_s;
  if (!run.started_at) return null;
  const end = run.finished_at ? new Date(run.finished_at).getTime() : Date.now();
  return (end - new Date(run.started_at).getTime()) / 1000;
}

const fullDate = (iso: string) => new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });

/** Council id → name, fetched once and only when some run uses a saved council. */
export function useCouncilNames(runs: Run[] | undefined): Map<string, string> {
  const needed = Boolean(runs?.some((r) => r.council_id));
  const q = useQuery({ queryKey: keys.councils, queryFn: () => api.get<Council[]>(`${V1}/councils`), enabled: needed });
  return useMemo(() => new Map((q.data ?? []).map((c) => [c.id, c.name])), [q.data]);
}

export const RunStatusCell = ({ run }: { run: Run }) => <StatusPill status={run.status} />;

export const RunModeCell = ({ run, className }: { run: Run; className?: string }) => <ModeLabel mode={run.mode} className={className} />;

/** A saved council's name, or "Auto council" when the roster came from the mode. */
export function RunCouncilCell({ run, names, className }: { run: Run; names: Map<string, string>; className?: string }) {
  const name = run.council_id ? names.get(run.council_id) ?? "Saved council" : "Auto council";
  return <span className={cn("truncate", className)} title={name}>{name}</span>;
}

export function RunCreatedCell({ run }: { run: Run }) {
  return <time dateTime={run.created_at} title={fullDate(run.created_at)}>{relTime(run.created_at)}</time>;
}

export const RunElapsedCell = ({ run }: { run: Run }) => <span className="tabular">{duration(runElapsed(run))}</span>;

/** Spend, plus a microbar against the run's dollar budget when it has one. */
export function RunCostCell({ run, className }: { run: Run; className?: string }) {
  const used = run.usage?.usd ?? 0;
  const cap = run.guardrails?.budget?.usd ?? null;
  const pct = cap ? Math.min(100, Math.round((used / cap) * 100)) : null;
  const tone = pct == null ? "" : pct >= 90 ? "bg-danger" : pct >= 70 ? "bg-warning" : "bg-accent";
  return (
    <span className={cn("inline-flex items-center gap-1.5 tabular", className)}>
      {pct != null && (
        <span role="meter" aria-label="Budget used" aria-valuemin={0} aria-valuemax={100} aria-valuenow={pct}
          aria-valuetext={`${usd(used)} of ${usd(cap)}`} title={`${usd(used)} of ${usd(cap)} budget`}
          className="h-1 w-8 shrink-0 rounded-full bg-surface-3 overflow-hidden">
          <span className={cn("block h-full", tone)} style={{ width: `${Math.max(pct, used > 0 ? 4 : 0)}%` }} />
        </span>
      )}
      {usd(run.usage?.usd)}
    </span>
  );
}

/** The stacked anatomy (Home, Runs on mobile): topic over a metadata line, status pill on the right. */
export function RunStackedBody({ run, names }: { run: Run; names: Map<string, string> }) {
  return (
    <>
      <div className="min-w-0 flex-1">
        <p className="truncate font-medium text-fg">{run.topic}</p>
        <p className="mt-0.5 flex flex-wrap items-center gap-x-1.5 gap-y-0.5 text-xs text-fg-subtle">
          <RunModeCell run={run} className="text-fg-muted" />
          <span aria-hidden>·</span>
          <RunCouncilCell run={run} names={names} className="max-w-[12rem]" />
          <span aria-hidden>·</span>
          <RunCreatedCell run={run} />
          <span aria-hidden>·</span>
          <RunElapsedCell run={run} />
          <span aria-hidden>·</span>
          <RunCostCell run={run} />
        </p>
      </div>
      <span className="shrink-0"><RunStatusCell run={run} /></span>
    </>
  );
}
