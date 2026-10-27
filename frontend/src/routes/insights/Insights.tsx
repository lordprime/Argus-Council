import { useQueryClient } from "@tanstack/react-query";
import { Activity, AlertOctagon, Clock, Coins, Download, Gauge, Hash, RefreshCw, ShieldAlert, type LucideIcon } from "lucide-react";
import { useMemo, useState, type ReactNode } from "react";

import {
  AreaChart, BarChart, ChartCard, color, HeatGrid, seriesTone, Sparkline, StackedBar, StatusDot, type Segment, type Tone,
} from "@/components/insights/charts";
import { InsightsPage } from "@/components/insights/InsightsPage";
import { Badge, Button, Card, CardTitle, EmptyState, ErrorState, Segmented, Skeleton } from "@/components/ui";
import {
  bucketRuns, downloadText, fmtDuration, fmtNum, fmtUsd, RANGES, rateTone, ruleSeverity, SEVERITIES, sortedEntries, type Bucket, type RangeValue,
} from "@/features/insights/logic";
import { useInsightRuns, useInsights, useProviderHealth, type Insights as InsightsData, type ProviderWithHealth } from "@/features/insights/queries";
import { cn } from "@/lib/cn";
import { modeMeta } from "@/lib/modes";
import type { Run } from "@/lib/types";

const STATUS_TONE: Record<string, Tone> = {
  completed: "success", failed: "danger", halted: "warning", running: "info", planning: "info",
  awaiting_approval: "accent", paused: "warning", queued: "neutral", cancelled: "neutral",
};

function KpiCard({ icon: Icon, label, value, sub, spark, tone, kind }: {
  icon: LucideIcon; label: string; value: string; sub: string; spark: number[]; tone: Tone; kind?: "line" | "bars";
}) {
  return (
    <Card as="article" className="p-4 flex flex-col gap-2 min-w-0" aria-label={label}>
      <div className="flex items-center justify-between gap-2">
        <h2 className="text-sm text-fg-muted font-medium">{label}</h2>
        <span className="size-7 rounded-full grid place-items-center" style={{ background: color(tone, 0.12), color: color(tone) }}>
          <Icon className="size-4" aria-hidden />
        </span>
      </div>
      <p className="text-2xl tabular" data-testid={`kpi-${label}`}>{value}</p>
      <p className="text-xs text-fg-subtle truncate">{sub}</p>
      <Sparkline values={spark} tone={tone} kind={kind} />
    </Card>
  );
}

function Panel({ title, description, children, className }: { title: string; description?: string; children: ReactNode; className?: string }) {
  return (
    <Card className={cn("p-4 flex flex-col gap-3 min-w-0", className)} aria-label={title}>
      <div>
        <CardTitle as="h2">{title}</CardTitle>
        {description && <p className="text-xs text-fg-subtle">{description}</p>}
      </div>
      {children}
    </Card>
  );
}

/** Sentence case for status strings from the server ("rate_limited" -> "Rate limited"). */
const sentence = (s: string) => { const t = s.replace(/_/g, " "); return t.charAt(0).toUpperCase() + t.slice(1).toLowerCase(); };

function healthTone(status: string | null | undefined): { tone: Tone; label: string } {
  if (!status) return { tone: "neutral", label: "Not probed" };
  const label = sentence(status);
  if (/^(ok|up|healthy|pass)/i.test(status)) return { tone: "success", label: /^ok$/i.test(status) ? "OK" : label };
  if (/degrad|slow|warn/i.test(status)) return { tone: "warning", label };
  return { tone: "danger", label };
}

function ModelTiles({ data, providers }: { data: InsightsData; providers: ProviderWithHealth[] }) {
  const byName = new Map(providers.map((p) => [p.name, p]));
  const seen = new Set<string>();
  const tiles = data.models.map((m) => {
    const prov = m.model.split("/")[0] ?? "";
    seen.add(prov);
    const p = byName.get(prov);
    const h = healthTone(p?.health_status);
    return { key: m.model, title: m.model, latency: p?.health_latency_ms ?? m.avg_latency_ms, calls: m.calls, ...h };
  });
  for (const p of providers) {
    if (seen.has(p.name)) continue;
    const h = p.enabled ? healthTone(p.health_status) : { tone: "neutral" as Tone, label: "Disabled" };
    tiles.push({ key: p.id, title: p.default_model ? `${p.name}/${p.default_model}` : p.name, latency: p.health_latency_ms ?? 0, calls: 0, ...h });
  }
  if (!tiles.length) return <p className="text-sm text-fg-muted">No model calls or providers yet.</p>;
  return (
    <ul className="grid grid-cols-1 min-[420px]:grid-cols-2 xl:grid-cols-3 gap-2">
      {tiles.map((t) => (
        <li key={t.key} className="rounded-lg border border-border bg-surface-2/60 p-3 min-w-0">
          <div className="flex items-center gap-2 min-w-0">
            <StatusDot tone={t.tone} label={`Status: ${t.label}`} />
            <p className="text-sm font-medium truncate" title={t.title}>{t.title}</p>
          </div>
          <dl className="mt-2 grid grid-cols-2 gap-1 text-xs">
            <div><dt className="text-fg-subtle">Latency</dt><dd className="tabular font-semibold">{t.latency ? `${fmtNum(t.latency)} ms` : "—"}</dd></div>
            <div><dt className="text-fg-subtle">Calls</dt><dd className="tabular font-semibold">{fmtNum(t.calls)}</dd></div>
          </dl>
          <p className="mt-1 text-xs text-fg-subtle">{t.label}</p>
        </li>
      ))}
    </ul>
  );
}

function ToolTiles({ data }: { data: InsightsData }) {
  if (!data.tools.length) return <p className="text-sm text-fg-muted">No tool calls in this window.</p>;
  return (
    <ul className="grid grid-cols-1 min-[420px]:grid-cols-2 xl:grid-cols-3 gap-2">
      {data.tools.map((t) => {
        const tone = rateTone(t.error_rate);
        return (
          <li key={t.tool} className="rounded-lg border border-border bg-surface-2/60 p-3 min-w-0">
            <div className="flex items-center gap-2 min-w-0">
              <StatusDot tone={tone} label={tone === "success" ? "Reliable" : tone === "warning" ? "Some errors" : "Unreliable"} />
              <p className="text-sm font-mono truncate" title={t.tool}>{t.tool}</p>
            </div>
            <dl className="mt-2 grid grid-cols-3 gap-1 text-xs">
              <div><dt className="text-fg-subtle">Errors</dt><dd className="tabular font-semibold" style={{ color: color(tone) }}>{t.error_rate}%</dd></div>
              <div><dt className="text-fg-subtle">Calls</dt><dd className="tabular font-semibold">{fmtNum(t.calls)}</dd></div>
              <div><dt className="text-fg-subtle">Avg</dt><dd className="tabular font-semibold">{fmtNum(t.avg_ms)} ms</dd></div>
            </dl>
            {t.blocked > 0 && <Badge tone="warning" className="mt-2">{t.blocked} blocked</Badge>}
          </li>
        );
      })}
    </ul>
  );
}

function Oversight({ data }: { data: InsightsData }) {
  const reasons = sortedEntries(data.oversight.intercepts_by_reason);
  const total = reasons.reduce((s, [, v]) => s + v, 0);
  const actions = sortedEntries(data.oversight.intercepts_by_action);
  const approvals = sortedEntries(data.oversight.approvals);
  return (
    <Panel title="Oversight" description={`${total} intercept${total === 1 ? "" : "s"} raised; ${approvals.reduce((s, [, v]) => s + v, 0)} approvals resolved`}>
      {reasons.length === 0 ? <p className="text-sm text-fg-muted">No intercepts in this window.</p> : (
        <ul className="flex flex-col gap-2.5" aria-label="Intercepts by reason">
          {reasons.map(([k, v], i) => {
            const tone = seriesTone(i);
            const pct = total ? Math.round((v / total) * 100) : 0;
            return (
              <li key={k} className="flex flex-col gap-1">
                <div className="flex justify-between gap-2 text-sm">
                  <span className="inline-flex items-center gap-1.5 min-w-0"><span aria-hidden className="size-2.5 rounded-sm shrink-0" style={{ background: color(tone) }} /><span className="truncate">{k}</span></span>
                  <span className="tabular text-fg-muted">{v} · {pct}%</span>
                </div>
                <div className="h-2 rounded-full bg-surface-2 overflow-hidden" role="progressbar" aria-label={k} aria-valuemin={0} aria-valuemax={100} aria-valuenow={pct}>
                  <div className="h-full rounded-full" style={{ width: `${pct}%`, background: color(tone) }} />
                </div>
              </li>
            );
          })}
        </ul>
      )}
      {actions.length > 0 && (
        <div>
          <h3 className="text-xs text-fg-subtle mb-1">Actions taken</h3>
          <ul className="flex flex-wrap gap-1.5">{actions.map(([k, v]) => <li key={k}><Badge tone="neutral">{k} · {v}</Badge></li>)}</ul>
        </div>
      )}
      <div>
        <h3 className="text-xs text-fg-subtle mb-1">Approvals</h3>
        {approvals.length === 0 ? <p className="text-sm text-fg-muted">None resolved.</p> : (
          <dl className="grid grid-cols-2 sm:grid-cols-3 gap-2">
            {approvals.map(([k, v]) => (
              <div key={k} className="rounded-lg bg-surface-2/60 p-2">
                <dt className="text-xs text-fg-subtle">{sentence(k)}</dt>
                <dd className="text-xl tabular">{v}</dd>
              </div>
            ))}
          </dl>
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

function Dashboard({ data, runs, providers, days }: { data: InsightsData; runs: Run[]; providers: ProviderWithHealth[]; days: number }) {
  const buckets = useMemo(() => bucketRuns(runs, days), [runs, days]);
  const t = data.totals;
  const tokens = t.tokens_in + t.tokens_out;
  const unit = days <= 1 ? "hour" : "day";
  const runsSeries = buckets.map((b) => b.runs);
  const peak = buckets.reduce<Bucket | undefined>((m, b) => (!m || b.runs > m.runs ? b : m), undefined);
  const models = data.models.map((m) => ({ label: m.model, value: m.cost_usd, display: fmtUsd(m.cost_usd), detail: `${m.calls} calls · ${fmtNum(m.tokens)} tokens` }));
  const statusSegs: Segment[] = sortedEntries(data.runs_by_status).map(([k, v]) => ({ label: k.replace(/_/g, " "), value: v, tone: STATUS_TONE[k] ?? "neutral" }));
  const modeSegs: Segment[] = sortedEntries(data.runs_by_mode).map(([k, v], i) => ({ label: modeMeta(k).label, value: v, tone: seriesTone(i) }));
  const grid = violationGrid(data, runs, buckets, days);
  const maxFail = Math.max(1, ...data.top_failures.map((f) => f.count));
  const window = `last ${days === 1 ? "24 hours" : `${days} days`}`;

  return (
    <div className="flex flex-col gap-4">
      <div className="grid grid-cols-1 min-[420px]:grid-cols-2 lg:grid-cols-3 xl:grid-cols-5 gap-3">
        <KpiCard icon={Activity} label="Runs" value={fmtNum(t.runs)} sub={`${data.analysed_runs} analysed · ${window}`} spark={runsSeries} tone="accent" kind="bars" />
        <KpiCard icon={Gauge} label="Success rate" value={`${t.success_rate}%`} sub={`${data.runs_by_status.completed ?? 0} completed`}
          spark={buckets.map((b) => (b.runs ? (b.completed / b.runs) * 100 : 0))} tone={t.success_rate >= 80 ? "success" : t.success_rate >= 50 ? "warning" : "danger"} />
        <KpiCard icon={Coins} label="Spend" value={fmtUsd(t.cost_usd)} sub={t.runs ? `${fmtUsd(t.cost_usd / t.runs)} per run` : "No runs"} spark={buckets.map((b) => b.usd)} tone="info" />
        <KpiCard icon={Hash} label="Tokens" value={fmtNum(tokens)} sub={`${fmtNum(t.tokens_in)} in · ${fmtNum(t.tokens_out)} out`} spark={buckets.map((b) => b.tokens)} tone="accent" kind="bars" />
        <KpiCard icon={Clock} label="Avg runtime" value={fmtDuration(t.avg_runtime_s)} sub="Per finished run" spark={buckets.map((b) => (b.runtimeN ? b.runtime / b.runtimeN : 0))} tone="neutral" />
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <ChartCard title="Spend by model" summary={models[0] ? `${models.length} models; ${models[0].label} cost the most at ${models[0].display}.` : "No model calls in this window."}
          table={{ columns: ["Model", "Spend", "Calls", "Tokens", "Avg latency"], rows: data.models.map((m) => [m.model, fmtUsd(m.cost_usd), m.calls, fmtNum(m.tokens), `${m.avg_latency_ms} ms`]) }}>
          {models.length ? <BarChart data={models} label={`Spend by model, ${window}`} /> : <p className="text-sm text-fg-muted">No model calls yet.</p>}
        </ChartCard>
        <ChartCard title="Runs over time"
          summary={peak && peak.runs ? `${runsSeries.reduce((a, b) => a + b, 0)} runs in view; busiest ${unit} ${peak.label} with ${peak.runs}.` : `No runs in the ${window}.`}
          table={{ columns: [days <= 1 ? "Hour" : "Day", "Runs", "Completed", "Failed", "Spend"], rows: buckets.map((b) => [b.label, b.runs, b.completed, b.failed, fmtUsd(b.usd)]) }}>
          <AreaChart label={`Runs per ${unit}, ${window}`} format={(v) => fmtNum(v)}
            points={buckets.map((b) => ({ label: b.label, value: b.runs, detail: `${b.completed} completed · ${b.failed} failed · ${fmtUsd(b.usd)}` }))} />
        </ChartCard>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <Panel title="Model & provider health" description="Latency from the provider health probe when available, otherwise the average call latency.">
          <ModelTiles data={data} providers={providers} />
        </Panel>
        <Panel title="Tool reliability" description="Error rate per tool. Green under 5%, amber under 20%, red above.">
          <ToolTiles data={data} />
        </Panel>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <Oversight data={data} />
        <ChartCard title="Run distribution" summary={`${t.runs} runs across ${statusSegs.length} statuses and ${modeSegs.length} modes.`}
          table={{ columns: ["Group", "Runs"], rows: [...statusSegs.map((s) => [`Status: ${s.label}`, s.value]), ...modeSegs.map((s) => [`Mode: ${s.label}`, s.value])] }}>
          <div className="flex flex-col gap-4">
            <div><h3 className="text-xs text-fg-subtle mb-1.5">By status</h3>
              <StackedBar segments={statusSegs} label={`Runs by status: ${statusSegs.map((s) => `${s.label} ${s.value}`).join(", ") || "none"}`} /></div>
            <div><h3 className="text-xs text-fg-subtle mb-1.5">By mode</h3>
              <StackedBar segments={modeSegs} label={`Runs by mode: ${modeSegs.map((s) => `${s.label} ${s.value}`).join(", ") || "none"}`} /></div>
          </div>
        </ChartCard>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <Panel title="Top failures" description="Failed and halted runs grouped by error.">
          {data.top_failures.length === 0 ? <p className="text-sm text-fg-muted">No failures in this window.</p> : (
            <ol className="flex flex-col divide-y divide-border">
              {data.top_failures.map((f) => (
                <li key={f.reason} className="flex items-center gap-3 py-2 min-w-0">
                  <AlertOctagon className="size-4 text-danger shrink-0" aria-hidden />
                  <div className="flex-1 min-w-0">
                    <p className="text-sm truncate" title={f.reason}>{f.reason}</p>
                    <div className="mt-1 h-1.5 rounded-full bg-surface-2 overflow-hidden" aria-hidden>
                      <div className="h-full rounded-full bg-danger" style={{ width: `${(f.count / maxFail) * 100}%` }} />
                    </div>
                  </div>
                  <span className="text-xl tabular shrink-0">{f.count}</span>
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
          <Panel title="Guardrail violations"><p className="text-sm text-fg-muted inline-flex items-center gap-2"><ShieldAlert className="size-4" aria-hidden />No guardrail violations in this window.</p></Panel>
        )}
      </div>
      {runs.length >= 200 && <p className="text-xs text-fg-subtle">Time series use the newest 200 runs; totals above cover every run in the window.</p>}
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
