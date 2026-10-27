import { History, Plus, RefreshCw, SearchX, Trash2, X } from "lucide-react";
import { useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router";

import { Button, Card, Dialog, EmptyState, ErrorState, PageHeader, SelectField, Skeleton, TextField } from "@/components/ui";
import { errorMessage } from "@/lib/api";
import { cn } from "@/lib/cn";
import { useRunsList } from "@/lib/queries";
import type { Run } from "@/lib/types";
import { TERMINAL, useDeleteRuns } from "@/features/runs/queries";
import { MODE_META, RunsStack, RunsTable, SelectAll } from "@/features/runs/RunsViews";
import { useMediaQuery } from "@/features/runs/useMediaQuery";
import { toast } from "@/stores/toast";

const STATUSES = ["queued", "running", "paused", "completed", "failed", "cancelled", "halted"] as const;
const PAGE = 100;
const MAX_LIMIT = 200; // backend cap for GET /runs?limit=

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;

const primaryLink =
  "inline-flex items-center gap-2 h-9 px-3 text-base rounded-md bg-accent text-accent-fg font-medium shadow-1 hover:bg-accent-hover transition-colors duration-fast";

function TableSkeleton() {
  return (
    <div aria-hidden className="divide-y divide-border">
      {Array.from({ length: 6 }, (_, i) => (
        <div key={i} className="flex items-center gap-4 px-4 py-3">
          <Skeleton className="size-4" />
          <div className="flex-1 space-y-1.5"><Skeleton className="h-4 w-2/3" /><Skeleton className="h-3 w-1/3" /></div>
          <Skeleton className="h-5 w-20 rounded-full" />
        </div>
      ))}
    </div>
  );
}

function DeleteDialog({ runs, open, onOpenChange, onDeleted }: {
  runs: Run[];
  open: boolean;
  onOpenChange: (o: boolean) => void;
  onDeleted: (ids: string[]) => void;
}) {
  const del = useDeleteRuns();
  const n = runs.length;
  const confirm = async () => {
    const res = await del.mutateAsync(runs.map((r) => r.id));
    if (res.deleted.length) toast({ tone: "success", title: `Deleted ${plural(res.deleted.length, "run")}` });
    if (res.failed.length) {
      toast({ tone: "danger", title: `${plural(res.failed.length, "run")} could not be deleted`, body: errorMessage(res.failed[0]!.error) });
    }
    onDeleted(res.deleted);
    onOpenChange(false);
  };
  return (
    <Dialog open={open} onOpenChange={onOpenChange} title={`Delete ${plural(n, "run")}?`}
      description="Their timelines, artifacts and exports are removed permanently. This can't be undone."
      footer={
        <>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button variant="danger" icon={Trash2} loading={del.isPending} onClick={confirm}>Delete {plural(n, "run")}</Button>
        </>
      }>
      <ul className="text-sm rounded-md border border-border divide-y divide-border max-h-56 overflow-auto">
        {runs.slice(0, 8).map((r) => (
          <li key={r.id} className="px-3 py-2 truncate">{r.topic}</li>
        ))}
        {n > 8 && <li className="px-3 py-2 text-fg-muted">and {plural(n - 8, "more run")}</li>}
      </ul>
    </Dialog>
  );
}

export default function RunsList() {
  const [params, setParams] = useSearchParams();
  const status = params.get("status") ?? "";
  const mode = params.get("mode") ?? "";
  const search = params.get("q") ?? "";
  const [limit, setLimit] = useState(PAGE);
  const q = useRunsList({ status, mode, limit });
  const wide = useMediaQuery("(min-width: 640px)");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [confirming, setConfirming] = useState(false);

  const setParam = (key: string, value: string) => {
    setParams((prev) => {
      const next = new URLSearchParams(prev);
      if (value) next.set(key, value);
      else next.delete(key);
      return next;
    }, { replace: key === "q" });
  };
  const clearFilters = () => setParams(new URLSearchParams());

  const runs = useMemo(() => {
    const needle = search.trim().toLowerCase();
    const all = q.data ?? [];
    return needle ? all.filter((r) => r.topic.toLowerCase().includes(needle)) : all;
  }, [q.data, search]);

  // Only act on runs that are visible and deletable right now.
  const chosen = runs.filter((r) => selected.has(r.id) && TERMINAL.has(r.status));
  const filtered = Boolean(status || mode || search);

  const toggle = (id: string, on: boolean) =>
    setSelected((s) => {
      const next = new Set(s);
      if (on) next.add(id);
      else next.delete(id);
      return next;
    });
  const toggleAll = (on: boolean) =>
    setSelected(on ? new Set(runs.filter((r) => TERMINAL.has(r.status)).map((r) => r.id)) : new Set());

  return (
    <div className="max-w-6xl mx-auto p-4 md:p-6">
      <PageHeader title="Runs" description="Every council run you've started, newest first."
        actions={
          <>
            <Button icon={RefreshCw} loading={q.isFetching && !q.isLoading} onClick={() => void q.refetch()}>Refresh</Button>
            <Link to="/" className={primaryLink}><Plus className="size-4" aria-hidden />New run</Link>
          </>
        } />

      <div role="search" aria-label="Filter runs" className="grid grid-cols-2 gap-3 sm:grid-cols-[minmax(0,1fr)_11rem_11rem_auto] items-end mb-4">
        <TextField label="Search topics" type="search" className="col-span-2 sm:col-span-1" placeholder="Filter by topic…" value={search}
          onChange={(e) => setParam("q", e.target.value)} />
        <SelectField label="Status" value={status} onChange={(e) => setParam("status", e.target.value)}>
          <option value="">All statuses</option>
          {STATUSES.map((s) => <option key={s} value={s}>{s[0]!.toUpperCase() + s.slice(1)}</option>)}
        </SelectField>
        <SelectField label="Mode" value={mode} onChange={(e) => setParam("mode", e.target.value)}>
          <option value="">All modes</option>
          {Object.entries(MODE_META).map(([k, m]) => <option key={k} value={k}>{m.label}</option>)}
        </SelectField>
        <Button variant="ghost" icon={X} onClick={clearFilters} disabled={!filtered}
          className={cn("justify-self-start", !filtered && "hidden sm:inline-flex")}>Clear</Button>
      </div>

      <div className="flex flex-wrap items-center gap-3 min-h-9 mb-2">
        {!wide && runs.some((r) => TERMINAL.has(r.status)) && (
          <span className="inline-flex items-center gap-1.5 text-sm text-fg-muted">
            <SelectAll runs={runs} selected={selected} onToggleAll={toggleAll} />
            <span aria-hidden>All finished</span>
          </span>
        )}
        <p className="text-xs text-fg-subtle tabular" aria-live="polite">
          {q.data ? (search ? `${runs.length} of ${plural(q.data.length, "run")} match` : plural(q.data.length, "run")) : " "}
        </p>
        {chosen.length > 0 && (
          <div className="ml-auto flex items-center gap-2">
            <span className="text-sm font-medium tabular">{chosen.length} selected</span>
            <Button size="sm" variant="ghost" onClick={() => setSelected(new Set())}>Clear selection</Button>
            <Button size="sm" variant="danger" icon={Trash2} onClick={() => setConfirming(true)}>Delete</Button>
          </div>
        )}
      </div>

      <Card as="div" className="overflow-hidden">
        {q.isLoading ? (
          <div role="status" aria-label="Loading runs"><TableSkeleton /></div>
        ) : q.error ? (
          <ErrorState error={q.error} onRetry={() => void q.refetch()} />
        ) : runs.length === 0 ? (
          filtered ? (
            <EmptyState icon={SearchX} title="No runs match these filters" body="Try another status or mode, or clear the search."
              action={<Button onClick={clearFilters} icon={X}>Clear filters</Button>} />
          ) : (
            <EmptyState icon={History} title="No runs yet" body="Start a run from Home and it will show up here."
              action={<Link to="/" className={primaryLink}><Plus className="size-4" aria-hidden />Start a run</Link>} />
          )
        ) : wide ? (
          <RunsTable runs={runs} selected={selected} onToggle={toggle} onToggleAll={toggleAll} />
        ) : (
          <RunsStack runs={runs} selected={selected} onToggle={toggle} />
        )}
      </Card>

      {q.data && q.data.length >= limit && (
        <div className="flex justify-center mt-4">
          {limit < MAX_LIMIT ? (
            <Button onClick={() => setLimit((l) => Math.min(MAX_LIMIT, l + PAGE))} loading={q.isFetching}>Load more</Button>
          ) : (
            <p className="text-xs text-fg-subtle">Showing the latest {MAX_LIMIT} runs. Use the filters to narrow down.</p>
          )}
        </div>
      )}

      <DeleteDialog runs={chosen} open={confirming && chosen.length > 0} onOpenChange={setConfirming}
        onDeleted={(ids) => setSelected((s) => new Set([...s].filter((id) => !ids.includes(id))))} />
    </div>
  );
}
