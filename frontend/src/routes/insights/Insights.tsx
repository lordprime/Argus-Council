import { useQueryClient } from "@tanstack/react-query";
import {
  Activity, AlertOctagon, ArrowDownRight, ArrowUpRight, Clock, Coins, Download, Gauge, Hash, Minus, RefreshCw, ShieldAlert, type LucideIcon,
} from "lucide-react";
import { useMemo, useState, type ReactNode } from "react";
import { Link } from "react-router";

import {
  AreaChart, BarChart, ChartCard, HeatGrid, Meter, Sparkline, StackedBar, StackedColumns, StatusDot, type Column, type ColumnKey, type Segment,
  type Tone,
} from "@/components/insights/charts";
import { InsightsPage } from "@/components/insights/InsightsPage";
import { Badge, Button, Card, CardTitle, EmptyState, ErrorState, Segmented, Skeleton, StatusPill } from "@/components/ui";
import {
  bucketRuns, delta, downloadText, fmtDuration, fmtNum, fmtUsd, RANGES, rateTone, ruleSeverity, seriesBuckets, SEVERITIES, sortedEntries, totalTokens,
  type Bucket, type Delta, type RangeValue,
} from "@/features/insights/logic";
import { useInsightRuns, useInsights, useProviderHealth, type Insights as InsightsData, type ProviderWithHealth } from "@/features/insights/queries";
import { cn } from "@/lib/cn";
import { relTime } from "@/lib/format";
import { modeMeta } from "@/lib/modes";
import type { Run } from "@/lib/types";

const STATUS_TONE: Record<string, Tone> = {
  completed: "success", failed: "danger", halted: "warning", running: "info", planning: "info",
  awaiting_approval: "accent", paused: "warning", queued: "neutral", cancelled: "neutral",
};
const LIVE = new Set(["queued", "running", "paused"]);
const OUTCOMES: ColumnKey[] = [
  { key: "completed", label: "Completed", tone: "success" },
  { key: "failed", label: "Failed", tone: "danger" },
  { key: "halted", label: "Halted", tone: "warning" },
  { key: "other", label: "Other", tone: "neutral" },
];

/** Sentence case for status strings from the server ("rate_limited" -> "Rate limited"). */
const sentence = (s: string) => { const t = s.replace(/_/g, " "); return t.charAt(0).toUpperCase() + t.slice(1).toLowerCase(); };

// ---------------------------------------------------------------- KPI tiles
/** Which way is good for a delta: success up is good; amounts (runs, spend, tokens) are not judged. */
type Better = "up" | "down" | null;

function KpiTile({ icon: Icon, label, value, change, better, prior, spark, kind }: {
  icon: LucideIcon; label: string; value: string; change: Delta | null; better: Better; prior: string; spark: number[]; kind?: "line" | "bars";
}) {
  const good = change && better && change.direction !== "flat" ? change.direction === better : null;
  const Arrow = !change || change.direction === "flat" ? Minus : change.direction === "up" ? ArrowUpRight : ArrowDownRight;
  return (
    <Card as="article" className="flex min-w-0 flex-col gap-1.5 p-4" aria-label={label}>
      <h2 className="flex items-center gap-1.5 text-sm font-medium text-fg-muted">
        <Icon className="size-3.5 text-fg-subtle" aria-hidden />{label}
      </h2>
      {/* Proportional sans figures at display size (tabular spacing reads loose this large). */}
      <p className="text-[1.75rem] font-semibold leading-tight tracking-tight" data-testid={`kpi-${label}`}>{value}</p>
      <p className="flex min-h-5 items-center gap-1 text-xs text-fg-muted">
        {change ? (
          <>
            <Arrow className={cn("size-3.5 shrink-0", good === true ? "text-success" : good === false ? "text-danger" : "text-fg-subtle")} aria-hidden />
            <span className="font-medium text-fg">{change.text}</span>
            <span className="truncate">vs {prior}</span>
            {good != null && <span className="sr-only">{good ? "(better)" : "(worse)"}</span>}
          </>
        ) : <span className="text-fg-subtle">No earlier period to compare</span>}
      </p>
      <div className="mt-auto pt-1"><Sparkline values={spark} kind={kind} /></div>
    </Card>
  );
}

function Panel({ title, description, children, className, actions }: {
  title: string; description?: string; children: ReactNode; className?: string; actions?: ReactNode;
}) {
  return (
    <Card className={cn("flex min-w-0 flex-col gap-3 p-4", className)} aria-label={title}>
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <CardTitle as="h2">{title}</CardTitle>
          {description && <p className="text-xs text-fg-subtle">{description}</p>}
        </div>
        {actions}
      </div>
      {children}
    </Card>
  );
}

// ---------------------------------------------------------------- Right now
function NowPanel({ runs }: { runs: Run[] }) {
  const live = runs.filter((r) => LIVE.has(r.status));
  const waiting = (r: Run) => (r.pending_approvals ?? 0) > 0;
  const count = (pred: (r: Run) => boolean) => live.filter(pred).length;
  const stats = [
    { label: "Working", value: count((r) => r.status !== "paused" && !waiting(r)) },
    { label: "Waiting for you", value: count(waiting) },
    { label: "Paused", value: count((r) => r.status === "paused") },
  ];
  // Runs waiting for an answer first.
  const ordered = [...live].sort((a, b) => Number(waiting(b)) - Number(waiting(a)));
  return (
    <Panel title="Right now" description="Runs that are still live."
      actions={<Link to="/runs" className="inline-flex min-h-6 items-center gap-0.5 text-xs text-fg-muted hover:text-accent">All runs<ArrowUpRight className="size-3.5" aria-hidden /></Link>}>
      <dl className="grid grid-cols-3 gap-px overflow-hidden rounded-lg border border-border bg-border">
        {stats.map((s) => (
          <div key={s.label} className="bg-surface px-3 py-2">
            <dt className="truncate text-xs text-fg-subtle">{s.label}</dt>
            <dd className={cn("text-xl font-semibold", s.label === "Waiting for you" && s.value > 0 && "text-accent")}>{s.value}</dd>
          </div>
        ))}
      </dl>
      {live.length === 0 ? (
        <p className="text-sm text-fg-muted">Nothing running. Start a run from <Link to="/" className="text-accent underline">Home</Link>.</p>
      ) : (
        <ul className="flex flex-col divide-y divide-border">
          {ordered.slice(0, 6).map((r) => (
            <li key={r.id} className="flex min-w-0 items-center gap-2 py-2">
              <Link to={`/runs/${r.id}`} className="min-w-0 flex-1 truncate text-sm hover:text-accent" title={r.topic}>{r.topic}</Link>
              {waiting(r) ? <StatusPill status="awaiting_approval" /> : <StatusPill status={r.status} />}
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}

// ---------------------------------------------------------------- Providers
function healthTone(status: string | null | undefined): { tone: Tone; label: string } {
  if (!status) return { tone: "neutral", label: "Not probed" };
  const label = sentence(status);
  if (/^(ok|up|healthy|pass)/i.test(status)) return { tone: "success", label: /^ok$/i.test(status) ? "OK" : label };
  if (/degrad|slow|warn/i.test(status)) return { tone: "warning", label };
  return { tone: "danger", label };
}

function ModelHealth({ data, providers }: { data: InsightsData; providers: ProviderWithHealth[] }) {
  const byName = new Map(providers.map((p) => [p.name, p]));
  const seen = new Set<string>();
  const rows = data.models.map((m) => {
    const prov = m.model.split("/")[0] ?? "";
    seen.add(prov);
    const p = byName.get(prov);
    return { key: m.model, title: m.model, latency: p?.health_latency_ms ?? m.avg_latency_ms, calls: m.calls, ...healthTone(p?.health_status) };
  });
  for (const p of providers) {
    if (seen.has(p.name)) continue;
    const h = p.enabled ? healthTone(p.health_status) : { tone: "neutral" as Tone, label: "Disabled" };
    rows.push({ key: p.id, title: p.default_model ? `${p.name}/${p.default_model}` : p.name, latency: p.health_latency_ms ?? 0, calls: 0, ...h });
  }
  if (!rows.length) return <p className="text-sm text-fg-muted">No model calls or providers yet.</p>;
  return (
    <div className="-mx-4 overflow-x-auto">
      <table className="w-full text-sm">
        <caption className="sr-only">Models and provider health</caption>
        <thead className="text-left text-xs text-fg-subtle">
          <tr className="border-b border-border">
            <th scope="col" className="px-4 py-1.5 font-medium">Model</th>
            <th scope="col" className="px-2 py-1.5 font-medium">Status</th>
            <th scope="col" className="px-2 py-1.5 text-right font-medium">Latency</th>
            <th scope="col" className="px-4 py-1.5 text-right font-medium">Calls</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-border">
          {rows.map((r) => (
            <tr key={r.key}>
              <th scope="row" className="max-w-[16rem] truncate px-4 py-2 text-left font-normal" title={r.title}>{r.title}</th>
              <td className="whitespace-nowrap px-2 py-2"><span className="inline-flex items-center gap-1.5"><StatusDot tone={r.tone} label="" />{r.label}</span></td>
              <td className="whitespace-nowrap px-2 py-2 text-right tabular">{r.latency ? `${fmtNum(r.latency)} ms` : "—"}</td>
              <td className="px-4 py-2 text-right tabular">{fmtNum(r.calls)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ---------------------------------------------------------------- Tools
const RELIABILITY: Record<Tone, string> = { success: "Reliable", warning: "Some errors", danger: "Unreliable", accent: "", info: "", neutral: "" };

function ToolTable({ data }: { data: InsightsData }) {
  if (!data.tools.length) return <p className="text-sm text-fg-muted">No tool calls in this window.</p>;
  return (
    <div className="-mx-4 overflow-x-auto">
      <table className="w-full min-w-[44rem] text-sm">
        <caption className="sr-only">Tool reliability, most error-prone first</caption>
        <thead className="text-left text-xs text-fg-subtle">
          <tr className="border-b border-border">
            <th scope="col" className="px-4 py-1.5 font-medium">Tool</th>
            <th scope="col" className="px-2 py-1.5 font-medium">Errors</th>
            <th scope="col" className="px-2 py-1.5 text-right font-medium">Calls</th>
            <th scope="col" className="px-2 py-1.5 text-right font-medium">Avg time</th>
            <th scope="col" className="px-2 py-1.5 text-right font-medium">Blocked</th>
            <th scope="col" className="px-4 py-1.5 font-medium">Most common error</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-border">
          {data.tools.map((t) => {
            const tone = rateTone(t.error_rate);
            const top = t.top_errors?.[0];
            return (
              <tr key={t.tool}>
                <th scope="row" className="whitespace-nowrap px-4 py-2 text-left font-mono font-normal">{t.tool}</th>
                <td className="whitespace-nowrap px-2 py-2">
                  <span className="inline-flex items-center gap-2">
                    <Meter value={t.error_rate} tone={tone} />
                    <span className="w-12 text-right tabular font-semibold">{t.error_rate}%</span>
                    <span className="text-xs text-fg-subtle">{RELIABILITY[tone]}</span>
                  </span>
                </td>
                <td className="px-2 py-2 text-right tabular">{fmtNum(t.calls)}</td>
                <td className="whitespace-nowrap px-2 py-2 text-right tabular">{fmtNum(t.avg_ms)} ms</td>
                <td className="px-2 py-2 text-right tabular">{t.blocked || "—"}</td>
                <td className="max-w-[22rem] px-4 py-2 text-xs text-fg-muted">
                  {top ? <span className="line-clamp-2 break-words" title={`${top.count} like this, for example: ${top.error}`}>{top.error} <span className="whitespace-nowrap text-fg-subtle">×{top.count}</span></span> : "—"}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// ---------------------------------------------------------------- Oversight
function Oversight({ data }: { data: InsightsData }) {
  const reasons = sortedEntries(data.oversight.intercepts_by_reason);
  const total = reasons.reduce((s, [, v]) => s + v, 0);
  const max = Math.max(1, ...reasons.map(([, v]) => v));
  const actions = sortedEntries(data.oversight.intercepts_by_action);
  const approvals = sortedEntries(data.oversight.approvals);
  const resolved = approvals.reduce((s, [, v]) => s + v, 0);
  const auto = data.oversight.approvals_auto ?? 0;
  return (
    <Panel title="Oversight" description={`${total} intercept${total === 1 ? "" : "s"} raised; ${resolved} approvals resolved`}>
      {reasons.length === 0 ? <p className="text-sm text-fg-muted">No intercepts in this window.</p> : (
        <ul className="flex flex-col gap-2" aria-label="Intercepts by reason">
          {reasons.map(([k, v]) => (
            <li key={k} className="grid grid-cols-[minmax(0,9rem)_1fr_auto] items-center gap-3 text-sm">
              <span className="truncate text-fg-muted" title={k}>{sentence(k)}</span>
              <span aria-hidden className="h-2.5 rounded-sm bg-accent" style={{ width: `${Math.max(3, (v / max) * 100)}%` }} />
              <span className="tabular">{v}</span>
            </li>
          ))}
        </ul>
      )}
      {actions.length > 0 && (
        <p className="text-xs text-fg-subtle">
          Actions taken: {actions.map(([k, v]) => `${sentence(k)} ${v}`).join(" · ")}
        </p>
      )}
      <div>
        <h3 className="mb-1.5 text-xs text-fg-subtle">Approvals</h3>
        {approvals.length === 0 ? <p className="text-sm text-fg-muted">None resolved.</p> : (
          <>
            <dl className="grid grid-cols-3 gap-px overflow-hidden rounded-lg border border-border bg-border">
              {approvals.map(([k, v]) => (
                <div key={k} className="bg-surface px-3 py-2">
                  <dt className="text-xs text-fg-subtle">{sentence(k)}</dt>
                  <dd className="text-xl font-semibold">{v}</dd>
                </div>
              ))}
            </dl>
            {auto > 0 && <p className="mt-1.5 text-xs text-fg-subtle">{auto} of {resolved} answered by auto mode.</p>}
          </>
        )}
      </div>
    </Panel>
  );
}

function violationGrid(data: InsightsData, runs: Run[], buckets: Bucket[], days: number) {
  const rules = sortedEntries(data.guardrail_violations).map(([k]) => k);
  const first = buckets[0];
  if (!rules.length) return null;
  // Per-period counts: halted/failed runs whose error names the rule.
  const step = first && buckets[1] ? buckets[1].start - first.start : 0;
  const perDay = rules.map(() => buckets.map(() => 0));
  let matched = 0;
  for (const r of runs) {
    const err = r.error?.toLowerCase();
    if (!err || !first || !step || (r.status !== "halted" && r.status !== "failed")) continue;
    const bi = Math.floor((Date.parse(r.created_at) - first.start) / step);
    if (!(bi >= 0 && bi < buckets.length)) continue;
    rules.forEach((rule, ri) => {
      const row = perDay[ri];
      if (row && err.includes(rule.toLowerCase())) {
        row[bi] = (row[bi] ?? 0) + 1;
        matched += 1;
      }
    });
  }
  if (matched > 0) {
    return { rows: rules, cols: buckets.map((b) => b.label), values: perDay,
      caption: `Halted or failed runs naming each rule, per ${days <= 1 ? "hour" : "day"}` };
  }
  return {
    rows: rules, cols: SEVERITIES.map((s) => s.charAt(0).toUpperCase() + s.slice(1)),
    values: rules.map((rule) => SEVERITIES.map((s) => (ruleSeverity(rule) === s ? data.guardrail_violations[rule] ?? 0 : 0))),
    caption: "Violations per rule by severity (no per-day breakdown available for this window)",
  };
}

// ---------------------------------------------------------------- Costliest runs
function CostliestRuns({ runs, since }: { runs: Run[]; since: number }) {
  const top = runs
    .filter((r) => Date.parse(r.created_at) >= since && Number(r.usage?.usd ?? 0) > 0)
    .sort((a, b) => Number(b.usage?.usd ?? 0) - Number(a.usage?.usd ?? 0))
    .slice(0, 8);
  if (!top.length) return <p className="text-sm text-fg-muted">No spend in this window.</p>;
  return (
    <div className="-mx-4 overflow-x-auto">
      <table className="w-full min-w-[40rem] text-sm">
        <caption className="sr-only">The most expensive runs in this window</caption>
        <thead className="text-left text-xs text-fg-subtle">
          <tr className="border-b border-border">
            <th scope="col" className="px-4 py-1.5 font-medium">Run</th>
            <th scope="col" className="px-2 py-1.5 font-medium">Status</th>
            <th scope="col" className="px-2 py-1.5 text-right font-medium">Spend</th>
            <th scope="col" className="px-2 py-1.5 text-right font-medium">Tokens</th>
            <th scope="col" className="px-2 py-1.5 text-right font-medium">Runtime</th>
            <th scope="col" className="px-4 py-1.5 text-right font-medium">Started</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-border">
          {top.map((r) => (
            <tr key={r.id}>
              <th scope="row" className="max-w-[24rem] px-4 py-2 text-left font-normal">
                <Link to={`/runs/${r.id}`} className="block truncate hover:text-accent" title={r.topic}>{r.topic}</Link>
                <span className="text-xs text-fg-subtle">{modeMeta(r.mode).label}</span>
              </th>
              <td className="px-2 py-2"><StatusPill status={r.status} /></td>
              <td className="px-2 py-2 text-right tabular font-semibold">{fmtUsd(Number(r.usage?.usd ?? 0))}</td>
              <td className="px-2 py-2 text-right tabular">{fmtNum(Number(r.usage?.tokens_in ?? 0) + Number(r.usage?.tokens_out ?? 0))}</td>
              <td className="whitespace-nowrap px-2 py-2 text-right tabular">{fmtDuration(r.usage?.runtime_s ?? null)}</td>
              <td className="whitespace-nowrap px-4 py-2 text-right text-fg-muted">{relTime(r.created_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ---------------------------------------------------------------- Dashboard
function Dashboard({ data, runs, providers, days }: { data: InsightsData; runs: Run[]; providers: ProviderWithHealth[]; days: number }) {
  const buckets = useMemo(() => (data.series ? seriesBuckets(data.series, days) : bucketRuns(runs, days)), [data.series, runs, days]);
  const t = data.totals;
  const p = data.previous_totals;
  const unit = days <= 1 ? "hour" : "day";
  const window = `last ${days === 1 ? "24 hours" : `${days} days`}`;
  const prior = `prior ${days === 1 ? "24 hours" : `${days} days`}`;
  const runsSeries = buckets.map((b) => b.runs);
  const peak = buckets.reduce<Bucket | undefined>((m, b) => (!m || b.runs > m.runs ? b : m), undefined);
  const models = data.models.map((m) => ({ label: m.model, value: m.cost_usd, display: fmtUsd(m.cost_usd), detail: `${m.calls} calls · ${fmtNum(m.tokens)} tokens` }));
  const statusSegs: Segment[] = sortedEntries(data.runs_by_status).map(([k, v]) => ({ label: sentence(k), value: v, tone: STATUS_TONE[k] ?? "neutral" }));
  const modes = sortedEntries(data.runs_by_mode).map(([k, v]) => ({ label: modeMeta(k).label, value: v, display: String(v) }));
  const grid = violationGrid(data, runs, buckets, days);
  const maxFail = Math.max(1, ...data.top_failures.map((f) => f.count));
  const flaky = data.tools.filter((x) => x.error_rate >= 20).length;
  const columns: Column[] = buckets.map((b) => ({
    label: b.label,
    values: { completed: b.completed, failed: b.failed - b.halted, halted: b.halted, other: Math.max(0, b.runs - b.completed - b.failed) },
    detail: b.usd ? `${fmtUsd(b.usd)} spent` : undefined,
  }));
  const spentInView = buckets.reduce((s, b) => s + b.usd, 0);

  return (
    <div className="flex flex-col gap-4">
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-3 xl:grid-cols-5">
        <KpiTile icon={Activity} label="Runs" value={fmtNum(t.runs)} change={delta(t.runs, p?.runs)} better={null} prior={prior} spark={runsSeries} kind="bars" />
        <KpiTile icon={Gauge} label="Success rate" value={`${t.success_rate}%`} change={p?.runs ? delta(t.success_rate, p.success_rate, "points") : null} better="up" prior={prior}
          spark={buckets.map((b) => (b.runs ? (b.completed / b.runs) * 100 : 0))} />
        <KpiTile icon={Coins} label="Spend" value={fmtUsd(t.cost_usd)} change={delta(t.cost_usd, p?.cost_usd)} better={null} prior={prior} spark={buckets.map((b) => b.usd)} />
        <KpiTile icon={Hash} label="Tokens" value={fmtNum(totalTokens(t))} change={p ? delta(totalTokens(t), totalTokens(p)) : null} better={null} prior={prior}
          spark={buckets.map((b) => b.tokens)} kind="bars" />
        <KpiTile icon={Clock} label="Avg runtime" value={fmtDuration(t.avg_runtime_s)} change={delta(t.avg_runtime_s, p?.avg_runtime_s)} better={null} prior={prior}
          spark={buckets.map((b) => (b.runtimeN ? b.runtime / b.runtimeN : 0))} />
      </div>

      <div className="grid gap-4 lg:grid-cols-3">
        <ChartCard className="lg:col-span-2" title="Runs by outcome"
          summary={peak && peak.runs ? `${runsSeries.reduce((a, b) => a + b, 0)} runs in the ${window}; busiest ${unit} ${peak.label} with ${peak.runs}.` : `No runs in the ${window}.`}
          table={{ columns: [days <= 1 ? "Hour" : "Day", "Runs", "Completed", "Failed", "Halted", "Spend"], rows: buckets.map((b) => [b.label, b.runs, b.completed, b.failed - b.halted, b.halted, fmtUsd(b.usd)]) }}>
          <StackedColumns columns={columns} keys={OUTCOMES} label={`Runs per ${unit} by outcome, ${window}`} />
        </ChartCard>
        <NowPanel runs={runs} />
      </div>

      <div className="grid gap-4 lg:grid-cols-3">
        <ChartCard className="lg:col-span-2" title="Spend over time"
          summary={spentInView ? `${fmtUsd(spentInView)} across the ${window}.` : `No spend in the ${window}.`}
          table={{ columns: [days <= 1 ? "Hour" : "Day", "Spend", "Tokens", "Runs"], rows: buckets.map((b) => [b.label, fmtUsd(b.usd), fmtNum(b.tokens), b.runs]) }}>
          <AreaChart label={`Spend per ${unit}, ${window}`} format={fmtUsd}
            points={buckets.map((b) => ({ label: b.label, value: b.usd, detail: `${b.runs} runs · ${fmtNum(b.tokens)} tokens` }))} />
        </ChartCard>
        <ChartCard title="Spend by model" summary={models[0] ? `${models.length} models; ${models[0].label} cost the most at ${models[0].display}.` : "No model calls in this window."}
          table={{ columns: ["Model", "Spend", "Calls", "Tokens", "Avg latency"], rows: data.models.map((m) => [m.model, fmtUsd(m.cost_usd), m.calls, fmtNum(m.tokens), `${m.avg_latency_ms} ms`]) }}>
          {models.length ? <BarChart data={models} label={`Spend by model, ${window}`} /> : <p className="text-sm text-fg-muted">No model calls yet.</p>}
        </ChartCard>
      </div>

      <Panel title="Tool reliability"
        description={flaky ? `${flaky} tool${flaky === 1 ? "" : "s"} failing on 20% or more of calls. Most error-prone first.` : "Error rate per tool: under 5% reliable, under 20% some errors, above that unreliable."}>
        <ToolTable data={data} />
      </Panel>

      <div className="grid gap-4 lg:grid-cols-2">
        <Panel title="Models and providers" description="Latency from the provider health probe when available, otherwise the average call latency.">
          <ModelHealth data={data} providers={providers} />
        </Panel>
        <Oversight data={data} />
      </div>

      <div className="grid gap-4 lg:grid-cols-3">
        <ChartCard title="Run mix" summary={`${t.runs} runs across ${statusSegs.length} statuses and ${modes.length} modes.`}
          table={{ columns: ["Group", "Runs"], rows: [...statusSegs.map((s) => [`Status: ${s.label}`, s.value]), ...modes.map((m) => [`Mode: ${m.label}`, m.value])] }}>
          <div className="flex flex-col gap-4">
            <div><h3 className="mb-1.5 text-xs text-fg-subtle">By status</h3>
              <StackedBar segments={statusSegs} label={`Runs by status: ${statusSegs.map((s) => `${s.label} ${s.value}`).join(", ") || "none"}`} /></div>
            <div><h3 className="mb-1.5 text-xs text-fg-subtle">By mode</h3>
              {modes.length ? <BarChart data={modes} label={`Runs by mode: ${modes.map((m) => `${m.label} ${m.value}`).join(", ")}`} /> : <p className="text-sm text-fg-muted">No runs.</p>}</div>
          </div>
        </ChartCard>
        <Panel title="Top failures" description="Failed and halted runs grouped by error.">
          {data.top_failures.length === 0 ? <p className="text-sm text-fg-muted">No failures in this window.</p> : (
            <ol className="flex flex-col divide-y divide-border">
              {data.top_failures.map((f) => (
                <li key={f.reason} className="flex min-w-0 items-center gap-3 py-2">
                  <AlertOctagon className="size-4 shrink-0 text-danger" aria-hidden />
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-sm" title={f.reason}>{f.reason}</p>
                    <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-surface-2" aria-hidden>
                      <div className="h-full rounded-full bg-danger" style={{ width: `${(f.count / maxFail) * 100}%` }} />
                    </div>
                  </div>
                  <span className="shrink-0 text-lg font-semibold tabular">{f.count}</span>
                </li>
              ))}
            </ol>
          )}
        </Panel>
        {grid ? (
          <ChartCard title="Guardrail violations" summary={`${Object.values(data.guardrail_violations).reduce((a, b) => a + b, 0)} violations across ${grid.rows.length} rules. ${grid.caption}.`}
            table={{ columns: ["Rule", ...grid.cols], rows: grid.rows.map((r, i) => [r, ...(grid.values[i] ?? [])]) }}>
            <HeatGrid rows={grid.rows} cols={grid.cols} values={grid.values} label={`Guardrail violations heat grid: ${grid.caption}`} />
          </ChartCard>
        ) : (
          <Panel title="Guardrail violations"><p className="inline-flex items-center gap-2 text-sm text-fg-muted"><ShieldAlert className="size-4" aria-hidden />No guardrail violations in this window.</p></Panel>
        )}
      </div>

      <Panel title="Costliest runs" description={`The runs that spent the most in the ${window}.`}>
        <CostliestRuns runs={runs} since={buckets[0]?.start ?? 0} />
      </Panel>
      {!data.series && runs.length >= 200 && <p className="text-xs text-fg-subtle">Time series use the newest 200 runs; totals above cover every run in the window.</p>}
    </div>
  );
}

export default function Insights() {
  const [range, setRange] = useState<RangeValue>("7");
  const days = Number(range);
  const qc = useQueryClient();
  const q = useInsights(days);
  const runs = useInsightRuns();
  const providers = useProviderHealth();

  const exportJson = () => {
    if (!q.data) return;
    downloadText(`argus-insights-${days}d-${new Date().toISOString().slice(0, 10)}.json`,
      JSON.stringify({ exported_at: new Date().toISOString(), ...q.data }, null, 2), "application/json");
  };
  const refresh = () => {
    void qc.invalidateQueries({ queryKey: ["insights"] });
    void qc.invalidateQueries({ queryKey: ["providers"] });
  };

  return (
    <InsightsPage title="Council insights"
      description="Spend, reliability and oversight across your runs."
      meta={
        <>
          <Badge tone="neutral">{q.data ? `${q.data.analysed_runs} runs analysed` : "Loading…"}</Badge>
          <Badge tone={q.isFetching ? "info" : "success"}>{q.isFetching ? "Updating" : "Up to date"}</Badge>
        </>
      }
      actions={
        <>
          <Button icon={Download} disabled={!q.data} onClick={exportJson}>Export JSON</Button>
          <Button icon={RefreshCw} loading={q.isFetching && !q.isLoading} onClick={refresh}>Refresh</Button>
        </>
      }>
      <Segmented label="Range" value={range} onChange={setRange} options={RANGES.map((r) => ({ value: r.value, label: r.label }))} />

      {q.isLoading ? (
        <div className="grid gap-3 grid-cols-1 min-[420px]:grid-cols-2 xl:grid-cols-5" aria-hidden>
          {Array.from({ length: 5 }, (_, i) => <Skeleton key={i} className="h-36" />)}
          <Skeleton className="h-64 min-[420px]:col-span-2 xl:col-span-5" />
        </div>
      ) : q.error ? (
        <Card><ErrorState error={q.error} onRetry={() => void q.refetch()} /></Card>
      ) : !q.data ? (
        <Card><EmptyState icon={Activity} title="No insights yet" /></Card>
      ) : (
        <Dashboard data={q.data} runs={runs.data ?? []} providers={providers.data ?? []} days={days} />
      )}
    </InsightsPage>
  );
}
