// Desktop table and mobile stacked list for /runs. Both share selection semantics:
// only terminal runs can be selected (the backend refuses to delete live runs). Both are built from
// the run cells Home uses (components/run/RunCells), so a run reads the same on every page.

import { useEffect, useRef, type InputHTMLAttributes, type ReactNode } from "react";
import { Link } from "react-router";

import { MODE_META } from "@/components/run/ModeLabel";
import {
  RunCostCell, RunCouncilCell, RunCreatedCell, RunElapsedCell, RunModeCell, RunStackedBody, RunStatusCell, runElapsed, useCouncilNames,
} from "@/components/run/RunCells";
import { cn } from "@/lib/cn";
import type { Run } from "@/lib/types";

import { TERMINAL } from "./queries";

export { MODE_META };
/** @deprecated use runElapsed from components/run/RunCells */
export const elapsed = runElapsed;

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

/**
 * The hit area around a native checkbox (WCAG 2.5.8): a <label>, so a click anywhere in it toggles
 * the box. `fill` stretches it over its table cell so the whole cell toggles.
 */
function HitArea({ disabled, fill, children }: { disabled?: boolean; fill?: boolean; children: ReactNode }) {
  return (
    <label className={cn("flex items-center justify-center min-h-6 min-w-6",
      fill ? "w-full self-stretch pl-3 pr-1 py-2.5 min-h-11" : "size-11 sm:size-8 -m-1",
      disabled ? "cursor-not-allowed" : "cursor-pointer")}>
      {children}
    </label>
  );
}

export interface SelectionProps {
  runs: Run[];
  selected: ReadonlySet<string>;
  onToggle: (id: string, on: boolean) => void;
}

function RowCheckbox({ run, selected, onToggle, fill }: { run: Run; fill?: boolean } & Omit<SelectionProps, "runs">) {
  const deletable = TERMINAL.has(run.status);
  return (
    <HitArea disabled={!deletable} fill={fill}>
      <Checkbox aria-label={`Select “${run.topic}”`} checked={selected.has(run.id)} disabled={!deletable}
        title={deletable ? undefined : "Only finished runs can be deleted"}
        onChange={(e) => onToggle(run.id, e.target.checked)} />
    </HitArea>
  );
}

export function SelectAll({ runs, selected, onToggleAll, fill }: {
  runs: Run[]; selected: ReadonlySet<string>; onToggleAll: (on: boolean) => void; fill?: boolean;
}) {
  const eligible = runs.filter((r) => TERMINAL.has(r.status));
  const count = eligible.filter((r) => selected.has(r.id)).length;
  return (
    <HitArea disabled={eligible.length === 0} fill={fill}>
      <Checkbox aria-label="Select all finished runs" disabled={eligible.length === 0}
        checked={eligible.length > 0 && count === eligible.length} indeterminate={count > 0 && count < eligible.length}
        onChange={(e) => onToggleAll(e.target.checked)} />
    </HitArea>
  );
}

export function RunsTable({ runs, selected, onToggle, onToggleAll }: SelectionProps & { onToggleAll: (on: boolean) => void }) {
  const names = useCouncilNames(runs);
  return (
    <div role="region" aria-label="Runs table" tabIndex={0} className="relative overflow-x-auto rounded-lg">
      <table className="w-full text-sm border-collapse">
        <caption className="sr-only">Runs, newest first. Only finished runs can be selected for deletion.</caption>
        <thead>
          <tr className="border-b border-border bg-surface-2/60 text-left text-xs font-medium text-fg-muted">
            <th scope="col" className="w-12 p-0"><div className="flex"><SelectAll runs={runs} selected={selected} onToggleAll={onToggleAll} fill /></div></th>
            <th scope="col" className="px-3 py-2 font-medium">Topic</th>
            <th scope="col" className="px-3 py-2 font-medium w-32">Mode</th>
            <th scope="col" className="hidden lg:table-cell px-3 py-2 font-medium w-40">Council</th>
            <th scope="col" className="px-3 py-2 font-medium w-40">Status</th>
            <th scope="col" className="hidden md:table-cell px-3 py-2 font-medium w-32">Created</th>
            <th scope="col" className="px-3 py-2 font-medium w-24 text-right">Duration</th>
            <th scope="col" className="pl-3 pr-4 py-2 font-medium w-28 text-right">Cost</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-border">
          {runs.map((run) => {
            const isSel = selected.has(run.id);
            return (
              <tr key={run.id} className={cn("transition-colors duration-fast", isSel ? "bg-accent-subtle/60" : "hover:bg-surface-2/70")}>
                <td className="p-0 align-middle"><div className="flex"><RowCheckbox run={run} selected={selected} onToggle={onToggle} fill /></div></td>
                <td className="px-3 py-2.5 max-w-0 w-full">
                  <Link to={`/runs/${run.id}`} className="block truncate font-medium text-fg hover:text-accent hover:underline underline-offset-2">
                    {run.topic}
                  </Link>
                  {run.description && <p className="truncate text-xs text-fg-subtle">{run.description}</p>}
                </td>
                <td className="px-3 py-2.5 whitespace-nowrap text-fg-muted"><RunModeCell run={run} /></td>
                <td className="hidden lg:table-cell px-3 py-2.5 text-fg-muted max-w-40"><RunCouncilCell run={run} names={names} className="block" /></td>
                <td className="px-3 py-2.5 whitespace-nowrap"><RunStatusCell run={run} /></td>
                <td className="hidden md:table-cell px-3 py-2.5 whitespace-nowrap text-fg-muted"><RunCreatedCell run={run} /></td>
                <td className="px-3 py-2.5 whitespace-nowrap text-right text-fg-muted"><RunElapsedCell run={run} /></td>
                <td className="pl-3 pr-4 py-2.5 whitespace-nowrap text-right"><RunCostCell run={run} className="justify-end" /></td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

/** Mobile: Home's stacked row anatomy plus a leading checkbox that sits outside the link. */
export function RunsStack({ runs, selected, onToggle }: SelectionProps) {
  const names = useCouncilNames(runs);
  return (
    <ul className="divide-y divide-border" aria-label="Runs">
      {runs.map((run) => (
        <li key={run.id} className={cn("flex items-center", selected.has(run.id) && "bg-accent-subtle/60")}>
          <span className="pl-1.5 flex"><RowCheckbox run={run} selected={selected} onToggle={onToggle} /></span>
          <Link to={`/runs/${run.id}`} className="flex flex-1 min-w-0 items-center gap-3 pl-1 pr-4 py-3 hover:bg-surface-2 focus-visible:bg-surface-2">
            <RunStackedBody run={run} names={names} />
          </Link>
        </li>
      ))}
    </ul>
  );
}
