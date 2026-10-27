import { ChevronDown, ChevronRight, Download, ScrollText } from "lucide-react";
import { Fragment, useState, type FormEvent } from "react";
import { Link } from "react-router";

import { Badge, Button, Card, EmptyState, ErrorState, IconButton, SelectField, Skeleton, TextField, Toggle } from "@/components/ui";
import { auditCsv, downloadText } from "@/features/insights/logic";
import { useAuditLog, type AuditEvent, type AuditFilters } from "@/features/insights/queries";
import { cn } from "@/lib/cn";
import { useSession } from "@/stores/session";

function when(ts: string) {
  return new Date(ts).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function Row({ e, showActor }: { e: AuditEvent; showActor: boolean }) {
  const [open, setOpen] = useState(false);
  const detailId = `audit-${e.id}`;
  const cols = showActor ? 7 : 6;
  return (
    <Fragment>
      <tr className={cn("hover:bg-surface-2", open && "bg-surface-2")}>
        <td className="px-3 py-2 whitespace-nowrap tabular" title={e.ts}>{when(e.ts)}</td>
        <td className="px-3 py-2 font-mono text-xs whitespace-nowrap">{e.action}</td>
        <td className="px-3 py-2 text-xs max-w-56 truncate" title={[e.target_type, e.target_id].filter(Boolean).join(" ")}>
          {e.target_type ? <><span className="text-fg-subtle">{e.target_type}</span> <span className="font-mono">{e.target_id}</span></> : <span className="text-fg-subtle">—</span>}
        </td>
        <td className="px-3 py-2"><Badge tone={e.success ? "success" : "danger"}>{e.success ? "Success" : "Failed"}</Badge></td>
        {showActor && <td className="px-3 py-2 font-mono text-xs whitespace-nowrap" title={e.actor_id ?? ""}>{e.actor_id ? e.actor_id.slice(0, 8) : "system"}</td>}
        <td className="px-3 py-2 font-mono text-xs whitespace-nowrap">{e.ip ?? "—"}</td>
        <td className="px-1 py-1">
          <IconButton label={`${open ? "Hide" : "Show"} details for event ${e.id}`} icon={ChevronDown} aria-expanded={open} aria-controls={open ? detailId : undefined}
            className={cn("[&_svg]:transition-transform", open && "[&_svg]:rotate-180")} onClick={() => setOpen((o) => !o)} />
        </td>
      </tr>
      {open && (
        <tr id={detailId} className="bg-surface-2">
          <td colSpan={cols} className="px-3 pb-3 pt-1">
            <dl className="grid gap-x-6 gap-y-2 md:grid-cols-2 text-xs">
              <div><dt className="text-fg-subtle">Event id</dt><dd className="font-mono">{e.id}</dd></div>
              <div><dt className="text-fg-subtle">Actor</dt><dd className="font-mono break-all">{e.actor_id ?? "system"}</dd></div>
              <div className="md:col-span-2"><dt className="text-fg-subtle">User agent</dt><dd className="font-mono break-all">{e.user_agent || "—"}</dd></div>
              <div className="md:col-span-2">
                <dt className="text-fg-subtle">Detail</dt>
                <dd><pre className="font-mono whitespace-pre-wrap break-all max-h-64 overflow-auto">{JSON.stringify(e.detail ?? {}, null, 2)}</pre></dd>
              </div>
            </dl>
          </td>
        </tr>
      )}
    </Fragment>
  );
}

export default function Audit() {
  const { user } = useSession();
  const isAdmin = Boolean(user?.is_admin || (user as { role?: string } | null)?.role === "admin");
  const [draft, setDraft] = useState("");
  const [filters, setFilters] = useState<AuditFilters>({ action: "", success: "", all: false });
  const q = useAuditLog(filters);
  const events = q.data?.pages.flat() ?? [];
  const showActor = filters.all;

  const apply = (ev: FormEvent) => {
    ev.preventDefault();
    setFilters((f) => ({ ...f, action: draft }));
  };

  return (
    <div className="p-4 md:p-6 max-w-[96rem] mx-auto flex flex-col gap-4 min-w-0">
      <nav aria-label="Breadcrumb" className="text-xs text-fg-subtle">
        <ol className="flex items-center gap-1">
          <li><Link to="/insights" className="hover:text-fg">Insights</Link></li>
          <li aria-hidden><ChevronRight className="size-3" /></li>
          <li aria-current="page" className="text-fg-muted">Audit log</li>
        </ol>
      </nav>
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl">Audit log</h1>
          <p className="text-sm text-fg-muted mt-1">Sign-ins, key changes, configuration edits and other security-relevant actions, newest first.</p>
        </div>
        <Button size="sm" icon={Download} disabled={!events.length}
          onClick={() => downloadText(`argus-audit-${new Date().toISOString().slice(0, 10)}.csv`, auditCsv(events), "text/csv")}>
          Export CSV
        </Button>
      </div>

      <Card className="p-3" aria-label="Filters">
        <form onSubmit={apply} className="flex flex-wrap items-end gap-3">
          <TextField label="Action contains" value={draft} onChange={(e) => setDraft(e.target.value)} placeholder="e.g. auth.login" className="w-full sm:w-64" />
          <SelectField label="Result" value={filters.success} className="w-full sm:w-40"
            onChange={(e) => setFilters((f) => ({ ...f, success: e.target.value as AuditFilters["success"] }))}>
            <option value="">Any</option>
            <option value="true">Success</option>
            <option value="false">Failed</option>
          </SelectField>
          <Button type="submit" variant="primary">Apply</Button>
          {isAdmin && (
            <div className="pb-1.5">
              <Toggle label="All users" hint="Administrators only" checked={filters.all} onChange={(v) => setFilters((f) => ({ ...f, all: v }))} />
            </div>
          )}
        </form>
      </Card>

      {q.isLoading ? (
        <Card as="div" className="p-3 space-y-2" aria-hidden>{Array.from({ length: 6 }, (_, i) => <Skeleton key={i} className="h-8" />)}</Card>
      ) : q.error ? (
        <Card><ErrorState error={q.error} onRetry={() => void q.refetch()} /></Card>
      ) : !events.length ? (
        <Card as="div"><EmptyState icon={ScrollText} title="No audit events match" body="Try a broader action filter or clear the result filter." /></Card>
      ) : (
        <Card as="div" className="overflow-hidden">
          <div tabIndex={0} role="region" aria-label="Audit events" className="overflow-x-auto">
            <table className="w-full text-sm">
              <caption className="sr-only">Audit events, newest first. Use the expand button on a row to see its detail.</caption>
              <thead className="bg-surface-2 text-left text-xs text-fg-subtle">
                <tr>
                  {["Time", "Action", "Target", "Result", ...(showActor ? ["Actor"] : []), "IP"].map((h) => (
                    <th key={h} scope="col" className="px-3 py-2 font-medium whitespace-nowrap">{h}</th>
                  ))}
                  <th scope="col" className="px-1 py-2"><span className="sr-only">Details</span></th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {events.map((e) => <Row key={e.id} e={e} showActor={showActor} />)}
              </tbody>
            </table>
          </div>
          <div className="flex flex-wrap items-center justify-between gap-2 px-3 py-2 border-t border-border text-xs text-fg-subtle">
            <span aria-live="polite">{events.length} event{events.length === 1 ? "" : "s"} loaded</span>
            {q.hasNextPage ? (
              <Button size="sm" loading={q.isFetchingNextPage} onClick={() => void q.fetchNextPage()}>Load more</Button>
            ) : <span>End of log</span>}
          </div>
        </Card>
      )}
    </div>
  );
}
