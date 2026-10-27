import { ArrowDownLeft, ArrowUpRight, ChevronDown, ScrollText, ShieldCheck, ShieldQuestion } from "lucide-react";
import { Fragment, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router";

import { ConnectionsPage } from "@/components/connections/ConnectionsNav";
import { Badge, Button, Callout, Card, EmptyState, ErrorState, FilterBar, IconButton, Segmented, SelectField, Skeleton } from "@/components/ui";
import { entryPurpose, useLedgerPage, useVerifyLedger, type LedgerPurpose } from "@/features/connections/queries";
import { errorMessage } from "@/lib/api";
import { cn } from "@/lib/cn";
import type { LedgerEntry } from "@/lib/types";

type ProtocolFilter = "all" | "mcp" | "a2a" | "acp";
type PurposeFilter = "all" | LedgerPurpose;
const PURPOSE: Record<LedgerPurpose, { label: string; tone: "info" | "accent" | "neutral"; hint: string }> = {
  run: { label: "Run", tone: "info", hint: "Calls made while a council run was working" },
  discover: { label: "Discover", tone: "accent", hint: "Connection tests and tool discovery (initialize, tools/list)" },
  refresh: { label: "Refresh", tone: "neutral", hint: "Agent card and ACP agent list refreshes" },
};
const LIMITS = [50, 100, 200, 500];

const short = (h: string, n = 10) => (h ? `${h.slice(0, n)}…` : "—");

function statusTone(s: string): "success" | "danger" | "warning" | "neutral" {
  if (s === "ok" || s === "completed") return "success";
  if (/error|fail|denied|blocked|timeout/i.test(s)) return "danger";
  if (/pending|working|submitted|held/i.test(s)) return "warning";
  return "neutral";
}

function when(ts: string) {
  return new Date(ts).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function VerifyPanel() {
  const verify = useVerifyLedger();
  const r = verify.data;
  const action = (
    <Button size="sm" variant={r?.ok === false ? "danger" : "secondary"} icon={ShieldCheck} loading={verify.isPending}
      onClick={() => verify.mutate()} className="self-start">
      {r ? "Verify again" : "Verify integrity"}
    </Button>
  );
  if (verify.error) {
    return <Callout tone="danger" title="Verification could not run" action={action}>{errorMessage(verify.error)}</Callout>;
  }
  if (!r) {
    return (
      <div className="flex flex-wrap items-center gap-3 rounded-lg border border-border bg-surface p-4">
        <ShieldQuestion className="size-6 text-fg-subtle shrink-0" aria-hidden />
        <div className="flex-1 min-w-48">
          <p className="font-semibold">Integrity not checked yet</p>
          <p className="text-sm text-fg-muted">Recomputes every entry's SHA-256 chain link from the first record to the head.</p>
        </div>
        {action}
      </div>
    );
  }
  return r.ok ? (
    <Callout tone="success" title={`Ledger intact · ${r.entries} ${r.entries === 1 ? "entry" : "entries"} verified`} action={action}>
      Head hash <span className="font-mono text-xs break-all text-fg">{r.head}</span>
    </Callout>
  ) : (
    <Callout tone="danger" title={`Chain broken at entry #${r.broken_at}`} action={action}>
      Reason: {r.reason}. Entries from #{r.broken_at} onward cannot be trusted. {r.entries} entries checked.
    </Callout>
  );
}

function Row({ e }: { e: LedgerEntry }) {
  const [open, setOpen] = useState(false);
  const inbound = e.direction === "inbound";
  const DirIcon = inbound ? ArrowDownLeft : ArrowUpRight;
  const detailId = `ledger-${e.seq}`;
  const purpose = entryPurpose(e);
  return (
    <Fragment>
      <tr className={cn("hover:bg-surface-2", open && "bg-surface-2")}>
        <td className="px-3 py-2 tabular text-fg-subtle">{e.seq}</td>
        <td className="px-3 py-2 whitespace-nowrap tabular" title={e.ts}>{when(e.ts)}</td>
        <td className="px-3 py-2"><Badge tone="accent">{e.protocol}</Badge></td>
        <td className="px-3 py-2 whitespace-nowrap">
          <span className={cn("inline-flex items-center gap-1", inbound ? "text-info" : "text-fg-muted")}>
            <DirIcon className="size-4" aria-hidden />{inbound ? "In" : "Out"}
          </span>
        </td>
        <td className="px-3 py-2 max-w-48 truncate font-mono text-xs" title={e.peer}>{e.peer}</td>
        <td className="px-3 py-2 whitespace-nowrap">
          <span className="inline-flex items-center h-6 rounded-sm border border-border bg-surface-2 px-1.5 font-mono text-xs">{e.method}</span>
        </td>
        <td className="px-3 py-2 whitespace-nowrap">
          {purpose ? <Badge tone={PURPOSE[purpose].tone}>{PURPOSE[purpose].label}</Badge> : <span className="text-fg-subtle">—</span>}
        </td>
        <td className="px-3 py-2"><Badge tone={statusTone(e.status)}>{e.status}</Badge></td>
        <td className="px-3 py-2 tabular text-right whitespace-nowrap">{e.latency_ms} ms</td>
        <td className="px-3 py-2 whitespace-nowrap">
          {e.run_id ? <Link to={`/runs/${e.run_id}`} className="text-accent underline underline-offset-2 font-mono text-xs" title={e.run_id}>{e.run_id.slice(0, 8)}</Link> : <span className="text-fg-subtle">—</span>}
        </td>
        <td className="px-3 py-2 font-mono text-xs whitespace-nowrap" title={e.hash}>{short(e.hash)}</td>
        <td className="px-1 py-1">
          <IconButton label={`${open ? "Hide" : "Show"} hashes for entry ${e.seq}`} icon={ChevronDown} aria-expanded={open} aria-controls={detailId}
            className={cn("[&_svg]:transition-transform", open && "[&_svg]:rotate-180")} onClick={() => setOpen((o) => !o)} />
        </td>
      </tr>
      {open && (
        <tr id={detailId} className="bg-surface-2">
          <td colSpan={12} className="px-3 pb-3 pt-1">
            <dl className="grid gap-x-6 gap-y-2 md:grid-cols-2 text-xs">
              {([["Entry hash", e.hash], ["Previous hash", e.prev_hash], ["Request SHA-256", e.request_sha256], ["Response SHA-256", e.response_sha256]] as const).map(([k, v]) => (
                <div key={k}>
                  <dt className="text-fg-subtle">{k}</dt>
                  <dd className="font-mono break-all">{v || "—"}</dd>
                </div>
              ))}
              <div className="md:col-span-2">
                <dt className="text-fg-subtle">Peer</dt>
                <dd className="font-mono break-all">{e.peer}</dd>
              </div>
              {Object.keys(e.detail ?? {}).length > 0 && (
                <div className="md:col-span-2">
                  <dt className="text-fg-subtle">Detail</dt>
                  <dd><pre className="font-mono whitespace-pre-wrap break-all max-h-48 overflow-auto">{JSON.stringify(e.detail, null, 2)}</pre></dd>
                </div>
              )}
            </dl>
          </td>
        </tr>
      )}
    </Fragment>
  );
}

export default function Ledger() {
  const [protocol, setProtocol] = useState<ProtocolFilter>("all");
  const [purpose, setPurpose] = useState<PurposeFilter>("all");
  const [limit, setLimit] = useState(100);
  const [params, setParams] = useSearchParams();
  const runId = params.get("run") ?? "";
  const setRun = (id: string) => setParams((p) => { const n = new URLSearchParams(p); if (id) n.set("run", id); else n.delete("run"); return n; }, { replace: true });
  const q = useLedgerPage(protocol === "all" ? undefined : protocol, limit, runId || undefined);
  const runs = useMemo(() => [...new Set([runId, ...(q.data ?? []).map((e) => e.run_id ?? "")].filter(Boolean))], [q.data, runId]);
  const rows = useMemo(() => (q.data ?? []).filter((e) => purpose === "all" || entryPurpose(e) === purpose), [q.data, purpose]);
  const filtered = protocol !== "all" || purpose !== "all" || Boolean(runId);

  return (
    <ConnectionsPage title="Protocol ledger"
      description="A tamper-evident, hash-chained record of every MCP, A2A and ACP call in and out of Argus.">
      <div className="flex flex-col gap-4">
        <VerifyPanel />
        <FilterBar label="Filter ledger entries" canClear={filtered}
          onClear={() => { setProtocol("all"); setPurpose("all"); setRun(""); }}>
          <Segmented label="Protocol" value={protocol} onChange={setProtocol} options={[
            { value: "all", label: "All" }, { value: "mcp", label: "MCP" }, { value: "a2a", label: "A2A" }, { value: "acp", label: "ACP" },
          ]} />
          <Segmented label="Purpose" value={purpose} onChange={setPurpose} options={[
            { value: "all", label: "All" },
            ...(Object.keys(PURPOSE) as LedgerPurpose[]).map((k) => ({ value: k, label: PURPOSE[k].label, hint: PURPOSE[k].hint })),
          ]} />
          <SelectField label="Run" value={runId} onChange={(e) => setRun(e.target.value)} className="w-full sm:w-44">
            <option value="">All runs</option>
            {runs.map((r) => <option key={r} value={r}>{r.slice(0, 8)}</option>)}
          </SelectField>
          <SelectField label="Show" value={String(limit)} onChange={(e) => setLimit(Number(e.target.value))} className="w-full sm:w-36">
            {LIMITS.map((n) => <option key={n} value={n}>Latest {n}</option>)}
          </SelectField>
        </FilterBar>
        {runId && (
          <p className="text-sm text-fg-muted flex flex-wrap items-center gap-2">
            Showing calls for run <Link to={`/runs/${runId}`} className="text-accent underline underline-offset-2 font-mono text-xs break-all">{runId}</Link>
            <Button size="sm" variant="ghost" onClick={() => setRun("")}>Clear run filter</Button>
          </p>
        )}

        {q.isLoading ? (
          <Card as="div" className="p-3 space-y-2" aria-hidden>
            {Array.from({ length: 6 }, (_, i) => <Skeleton key={i} className="h-8" />)}
          </Card>
        ) : q.error ? (
          <Card><ErrorState error={q.error} onRetry={() => void q.refetch()} /></Card>
        ) : !rows.length ? (
          <Card as="div">
            <EmptyState icon={ScrollText}
              title={!filtered ? "No protocol calls recorded yet" : "No calls match these filters"}
              body={!filtered
                ? "Calls to MCP servers and remote agents, and calls other systems make to Argus, are appended here with their hashes."
                : `Nothing${protocol === "all" ? "" : ` over ${protocol.toUpperCase()}`}${purpose === "all" ? "" : ` for ${PURPOSE[purpose].label.toLowerCase()}`}${runId ? " in this run" : ""} among the latest ${limit} entries.`} />
          </Card>
        ) : (
          <Card as="div" className="overflow-hidden">
            <div tabIndex={0} role="region" aria-label="Ledger entries" className="relative overflow-x-auto">
              <table className="w-full text-sm">
                <caption className="sr-only">Ledger entries, newest first. Use the expand button on a row to see full hashes.</caption>
                <thead className="bg-surface-2 text-left text-xs text-fg-subtle">
                  <tr>
                    {["#", "Time", "Protocol", "Direction", "Peer", "Method", "Purpose", "Status", "Latency", "Run", "Hash"].map((h) => (
                      <th key={h} scope="col" className={cn("px-3 py-2 font-medium whitespace-nowrap", h === "Latency" && "text-right")}>{h}</th>
                    ))}
                    <th scope="col" className="px-1 py-2"><span className="sr-only">Details</span></th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border">
                  {rows.map((e) => <Row key={e.seq} e={e} />)}
                </tbody>
              </table>
            </div>
            {(q.data?.length ?? 0) >= limit && (
              <p className="px-3 py-2 text-xs text-fg-subtle border-t border-border">Showing the latest {limit} entries. Choose a larger page to see more.</p>
            )}
          </Card>
        )}
      </div>
    </ConnectionsPage>
  );
}
