// Desktop table and mobile stacked list for /runs. Both share selection semantics:
// only terminal runs can be selected (the backend refuses to delete live runs).

import { Code2, FileText, MessageCircle, Wand2 } from "lucide-react";
import { useEffect, useRef, type InputHTMLAttributes } from "react";
import { Link } from "react-router";

import { StatusPill } from "@/components/ui";
import { cn } from "@/lib/cn";
import { duration, relTime, usd } from "@/lib/format";
import type { Mode, Run } from "@/lib/types";

import { TERMINAL } from "./queries";

export const MODE_META: Record<Mode, { label: string; icon: typeof FileText }> = {
  research: { label: "Research", icon: FileText },
  coding: { label: "Coding", icon: Code2 },
  chat: { label: "Chat", icon: MessageCircle },
  general: { label: "General", icon: Wand2 },
};

export function elapsed(run: Run): number | null {
  if (run.usage?.runtime_s != null) return run.usage.runtime_s;
  if (!run.started_at) return null;
  const end = run.finished_at ? new Date(run.finished_at).getTime() : Date.now();
  return (end - new Date(run.started_at).getTime()) / 1000;
}

const fullDate = (iso: string) => new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });

export function Checkbox({ indeterminate, className, ...rest }: InputHTMLAttributes<HTMLInputElement> & { indeterminate?: boolean }) {
  const ref = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (ref.current) ref.current.indeterminate = Boolean(indeterminate);
  }, [indeterminate]);
  return (
    <input ref={ref} type="checkbox"
      className={cn("size-4 cursor-pointer accent-[rgb(var(--accent))] disabled:cursor-not-allowed disabled:opacity-40", className)}
      {...rest} />
  );
}

export interface SelectionProps {
  runs: Run[];
  selected: ReadonlySet<string>;
  onToggle: (id: string, on: boolean) => void;
}

function RowCheckbox({ run, selected, onToggle }: { run: Run } & Omit<SelectionProps, "runs">) {
  const deletable = TERMINAL.has(run.status);
  return (
    // A 24px+ hit area around the native checkbox (WCAG 2.5.8).
    <span className="inline-flex size-6 items-center justify-center">
      <Checkbox aria-label={`Select “${run.topic}”`} checked={selected.has(run.id)} disabled={!deletable}
        title={deletable ? undefined : "Only finished runs can be deleted"}
        onChange={(e) => onToggle(run.id, e.target.checked)} />
    </span>
  );
}

export function SelectAll({ runs, selected, onToggleAll }: { runs: Run[]; selected: ReadonlySet<string>; onToggleAll: (on: boolean) => void }) {
  const eligible = runs.filter((r) => TERMINAL.has(r.status));
  const count = eligible.filter((r) => selected.has(r.id)).length;
  return (
    <span className="inline-flex size-6 items-center justify-center">
      <Checkbox aria-label="Select all finished runs" disabled={eligible.length === 0}
        checked={eligible.length > 0 && count === eligible.length} indeterminate={count > 0 && count < eligible.length}
        onChange={(e) => onToggleAll(e.target.checked)} />
    </span>
  );
}

export function RunsTable({ runs, selected, onToggle, onToggleAll }: SelectionProps & { onToggleAll: (on: boolean) => void }) {
  return (
    <div role="region" aria-label="Runs table" tabIndex={0} className="overflow-x-auto rounded-lg">
      <table className="w-full text-sm border-collapse">
        <caption className="sr-only">Runs, newest first. Only finished runs can be selected for deletion.</caption>
        <thead>
          <tr className="border-b border-border bg-surface-2/60 text-left text-xs font-medium text-fg-muted">
            <th scope="col" className="w-12 pl-3 pr-1 py-2"><SelectAll runs={runs} selected={selected} onToggleAll={onToggleAll} /></th>
            <th scope="col" className="px-3 py-2 font-medium">Topic</th>
            <th scope="col" className="px-3 py-2 font-medium w-32">Mode</th>
            <th scope="col" className="px-3 py-2 font-medium w-40">Status</th>
            <th scope="col" className="px-3 py-2 font-medium w-32">Created</th>
            <th scope="col" className="px-3 py-2 font-medium w-24 text-right">Duration</th>
            <th scope="col" className="pl-3 pr-4 py-2 font-medium w-24 text-right">Cost</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-border">
          {runs.map((run) => {
            const mode = MODE_META[run.mode] ?? MODE_META.general;
            const isSel = selected.has(run.id);
            return (
              <tr key={run.id} className={cn("transition-colors duration-fast", isSel ? "bg-accent-subtle/60" : "hover:bg-surface-2/70")}>
                <td className="pl-3 pr-1 py-2.5 align-middle"><RowCheckbox run={run} selected={selected} onToggle={onToggle} /></td>
                <td className="px-3 py-2.5 max-w-0 w-full">
                  <Link to={`/runs/${run.id}`} className="block truncate font-medium text-fg hover:text-accent hover:underline underline-offset-2">
                    {run.topic}
                  </Link>
                  {run.description && <p className="truncate text-xs text-fg-subtle">{run.description}</p>}
                </td>
                <td className="px-3 py-2.5 whitespace-nowrap text-fg-muted">
                  <span className="inline-flex items-center gap-1.5"><mode.icon className="size-4 text-fg-subtle" aria-hidden />{mode.label}</span>
                </td>
                <td className="px-3 py-2.5 whitespace-nowrap"><StatusPill status={run.status} /></td>
                <td className="px-3 py-2.5 whitespace-nowrap text-fg-muted">
                  <time dateTime={run.created_at} title={fullDate(run.created_at)}>{relTime(run.created_at)}</time>
                </td>
                <td className="px-3 py-2.5 whitespace-nowrap text-right tabular text-fg-muted">{duration(elapsed(run))}</td>
                <td className="pl-3 pr-4 py-2.5 whitespace-nowrap text-right tabular">{usd(run.usage?.usd)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

/** Mobile: the RunRow anatomy plus a leading checkbox that sits outside the link. */
export function RunsStack({ runs, selected, onToggle }: SelectionProps) {
  return (
    <ul className="divide-y divide-border" aria-label="Runs">
      {runs.map((run) => {
        const mode = MODE_META[run.mode] ?? MODE_META.general;
        return (
          <li key={run.id} className={cn("flex items-center", selected.has(run.id) && "bg-accent-subtle/60")}>
            <span className="pl-2.5"><RowCheckbox run={run} selected={selected} onToggle={onToggle} /></span>
            <Link to={`/runs/${run.id}`} className="flex flex-1 min-w-0 items-center gap-3 pl-2 pr-4 py-3 hover:bg-surface-2 focus-visible:bg-surface-2">
              <div className="min-w-0 flex-1">
                <p className="truncate font-medium">{run.topic}</p>
                <p className="text-xs text-fg-subtle tabular flex items-center gap-1 flex-wrap">
                  <mode.icon className="size-3.5" aria-hidden />
                  {mode.label} · {relTime(run.created_at)} · {duration(elapsed(run))} · {usd(run.usage?.usd)}
                </p>
              </div>
              <StatusPill status={run.status} />
            </Link>
          </li>
        );
      })}
    </ul>
  );
}
