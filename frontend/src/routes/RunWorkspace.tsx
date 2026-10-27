import { useQueryClient } from "@tanstack/react-query";
import { ArrowDown, Download, FileText, FolderTree, Pause, Play, Square } from "lucide-react";
import { Component, Suspense, lazy, useCallback, useEffect, useMemo, useRef, useState, type ComponentType, type ReactNode } from "react";
import { Link, useLocation, useParams } from "react-router";

import {
  ApprovalCard,
  BudgetMeter,
  CouncilRail,
  InterventionBar,
  PlanCard,
  SimpleRow,
  ThoughtCard,
  ToolChip,
  VoteCard,
} from "@/components/run/parts";
import { ReplayBar, useReplayView } from "@/components/run/ReplayBar";
import { Badge, Button, Callout, EmptyState, ErrorState, SegmentedTabs, Skeleton, StatusPill, Tabs } from "@/components/ui";
import { Markdown } from "@/components/ui/Markdown";
import { api, auth, errorMessage, V1 } from "@/lib/api";
import { cn } from "@/lib/cn";
import { clock, duration, usd } from "@/lib/format";
import { keys, useArtifacts, useRunDetail } from "@/lib/queries";
import { openRunStream } from "@/lib/stream";
import type { OutputFormat, RunDetail } from "@/lib/types";
import { isTerminal, useRun, useRuns, type RunState, type TimelineItem } from "@/stores/run";
import { toast } from "@/stores/toast";

type Density = "decisions" | "working" | "everything";

/* ------------------------------------------------------------ view switcher */

export type VizSelect = (ref: { itemId?: string; nodeId?: string }) => void;
type VizProps = { run: RunState; onSelect?: VizSelect };

const CouncilMap = lazy(() => import("@/components/viz/map/CouncilMap"));
const RunGantt = lazy(() => import("@/components/viz/gantt/RunGantt"));
const CommsHeatmap = lazy(() => import("@/components/viz/heatmap/CommsHeatmap"));
const RunRoute = lazy(() => import("@/components/viz/route/RunRoute"));

type RunView = "timeline" | "map" | "gantt" | "heatmap" | "route";
const VIEWS: { value: RunView; label: string; component?: ComponentType<VizProps> }[] = [
  { value: "timeline", label: "Timeline" },
  { value: "map", label: "Map", component: CouncilMap },
  { value: "gantt", label: "Gantt", component: RunGantt },
  { value: "heatmap", label: "Heatmap", component: CommsHeatmap },
  { value: "route", label: "Route", component: RunRoute },
];
const VIEW_KEY = "argus.runView";

function loadView(): RunView {
  try {
    const v = localStorage.getItem(VIEW_KEY);
    if (v && VIEWS.some((x) => x.value === v)) return v as RunView;
  } catch {
    /* storage unavailable */
  }
  return "timeline";
}

function saveView(v: RunView) {
  try {
    localStorage.setItem(VIEW_KEY, v);
  } catch {
    /* storage unavailable */
  }
}

class VizBoundary extends Component<{ children: ReactNode }, { error: unknown }> {
  state = { error: null as unknown };
  static getDerivedStateFromError(error: unknown) {
    return { error };
  }
  render() {
    if (this.state.error) return <div className="p-4"><ErrorState error={this.state.error} onRetry={() => this.setState({ error: null })} /></div>;
    return this.props.children;
  }
}

function visible(item: TimelineItem, density: Density): boolean {
  if (density === "everything") return true;
  if (item.kind === "system") return item.type === "run.status" && density === "working";
  if (density === "decisions") return !["tool", "citation"].includes(item.kind);
  return true;
}

function useRunStream(runId: string, detail: RunDetail | undefined) {
  const init = useRuns((s) => s.init);
  const apply = useRuns((s) => s.apply);
  const setConnection = useRuns((s) => s.setConnection);
  const qc = useQueryClient();
  useEffect(() => {
    if (!detail) return;
    init(runId, detail.council_snapshot.members ?? []);
    return openRunStream(runId, {
      onEvents: (events) => {
        apply(runId, events);
        if (events.some((e) => e.type === "run.status")) {
          void qc.invalidateQueries({ queryKey: keys.run(runId) });
          void qc.invalidateQueries({ queryKey: ["runs"] });
        }
        if (events.some((e) => e.type.startsWith("artifact."))) void qc.invalidateQueries({ queryKey: keys.artifacts(runId) });
      },
      onState: (st, attempt) => setConnection(runId, st, attempt),
      getLastSeq: () => useRuns.getState().runs[runId]?.lastSeq ?? 0,
    });
  }, [runId, detail?.id]); // eslint-disable-line react-hooks/exhaustive-deps
}

function Header({ run, runId }: { run: RunDetail; runId: string }) {
  const status = useRun(runId, (s) => (s.lastSeq ? s.status : run.status));
  const budget = useRun(runId, (s) => s.budget);
  const [busy, setBusy] = useState<string | null>(null);
  const act = async (action: "pause" | "resume" | "cancel") => {
    if (action === "cancel" && !window.confirm("Stop this run? Artifacts produced so far are kept.")) return;
    setBusy(action);
    try {
      await api.post(`${V1}/runs/${runId}/${action}`);
    } catch (err) {
      toast({ tone: "danger", title: `Could not ${action}`, body: errorMessage(err) });
    } finally {
      setBusy(null);
    }
  };
  const live = !isTerminal(status);
  const loc = useLocation();
  const onOutput = loc.pathname.endsWith("/output");
  return (
    <div className="border-b border-border bg-surface px-4 py-3 flex flex-wrap items-center gap-x-4 gap-y-2">
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <StatusPill status={status} />
          <Badge>{run.mode}</Badge>
          <span className="text-xs text-fg-subtle truncate">{run.council_snapshot.name}</span>
        </div>
        <h1 className="text-xl truncate mt-1" title={run.topic}>{run.topic}</h1>
      </div>
      <BudgetMeter budget={budget} />
      <div className="flex items-center gap-2">
        {live && (status === "paused" ? (
          <Button icon={Play} loading={busy === "resume"} onClick={() => void act("resume")}>Resume</Button>
        ) : (
          <Button icon={Pause} loading={busy === "pause"} onClick={() => void act("pause")}>Pause</Button>
        ))}
        {live && <Button variant="danger-ghost" icon={Square} loading={busy === "cancel"} onClick={() => void act("cancel")}>Stop</Button>}
        <nav aria-label="Run views" className="inline-flex rounded-md bg-surface-2 p-0.5">
          <Link to={`/runs/${runId}`} aria-current={!onOutput ? "page" : undefined}
            className={cn("px-3 h-8 inline-flex items-center rounded text-sm", !onOutput ? "bg-surface shadow-1 font-medium" : "text-fg-muted")}>Timeline</Link>
          <Link to={`/runs/${runId}/output`} aria-current={onOutput ? "page" : undefined}
            className={cn("px-3 h-8 inline-flex items-center rounded text-sm", onOutput ? "bg-surface shadow-1 font-medium" : "text-fg-muted")}>Output</Link>
        </nav>
      </div>
    </div>
  );
}

function Banner({ run, runId }: { run: RunDetail; runId: string }) {
  const status = useRun(runId, (s) => (s.lastSeq ? s.status : run.status));
  const reason = useRun(runId, (s) => s.statusReason);
  const connection = useRun(runId, (s) => s.connection);
  const attempt = useRun(runId, (s) => s.reconnectAttempt);
  if (connection === "reconnecting") return <div className="px-4 py-1.5 text-sm bg-warning-subtle text-warning" role="status">Reconnecting… (attempt {attempt})</div>;
  if (connection === "lost") return <div className="px-4 py-1.5 text-sm bg-danger-subtle text-danger" role="alert">Connection lost. The run continues on the server. <button className="underline" onClick={() => window.location.reload()}>Retry</button></div>;
  if (status === "completed") {
    return (
      <div className="px-4 py-2 bg-success-subtle text-sm flex items-center gap-3" role="status">
        <span className="text-success font-semibold">Completed</span>
        <span className="text-fg-muted tabular">{duration(run.usage?.runtime_s)} · {usd(run.usage?.usd)} · {run.usage?.steps ?? 0} steps</span>
        <Link to={`/runs/${runId}/output`} className="ml-auto text-accent underline">Open deliverable</Link>
      </div>
    );
  }
  if (status === "failed" || status === "halted") {
    return <div className="px-4 py-2 bg-danger-subtle text-sm" role="alert"><span className="text-danger font-semibold">{status === "halted" ? "Halted by guardrails" : "Failed"}</span>: {reason ?? run.error}</div>;
  }
  if (status === "paused") return <div className="px-4 py-2 bg-warning-subtle text-sm" role="status">Paused. Agents stop at their next step. Messages you send are queued.</div>;
  return null;
}

function Timeline({ runId, density, focus, onSelect, selected, reveal, onRevealed }: {
  runId: string; density: Density; focus: string | null; onSelect: (id: string) => void; selected: string | null;
  /** item id to scroll into view and flash once (set by a visualisation's onSelect) */
  reveal?: string | null; onRevealed?: () => void;
}) {
  const order = useRun(runId, (s) => s.order);
  const items = useRun(runId, (s) => s.items);
  const members = useRun(runId, (s) => s.members);
  const plan = useRun(runId, (s) => s.plan);
  const status = useRun(runId, (s) => s.status);
  const live = !isTerminal(status);
  const ref = useRef<HTMLDivElement>(null);
  const [atBottom, setAtBottom] = useState(true);
  const [unseen, setUnseen] = useState(0);
  const shown = useMemo(
    () => order.map((id) => items[id]!).filter((it) => it && visible(it, density) &&
      (!focus || ("actor" in it && it.actor?.member_id === focus) || it.kind === "plan" || it.kind === "approval")),
    [order, items, density, focus],
  );
  const pending = shown.filter((i) => i.kind === "approval" && i.status === "pending");
  const prev = useRef(shown.length);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const added = shown.length - prev.current;
    prev.current = shown.length;
    if (atBottom) el.scrollTop = el.scrollHeight;
    else if (added > 0) setUnseen((u) => u + added);
  }, [shown, atBottom]);
  const [flash, setFlash] = useState<string | null>(null);
  useEffect(() => {
    if (!reveal) return;
    const el = document.getElementById(`run-item-${reveal}`);
    if (el) {
      el.scrollIntoView({ block: "center" });
      setFlash(reveal);
    }
    onRevealed?.();
  }, [reveal, shown]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (!flash) return;
    const t = window.setTimeout(() => setFlash(null), 2000);
    return () => window.clearTimeout(t);
  }, [flash]);
  const onScroll = () => {
    const el = ref.current;
    if (!el) return;
    const bottom = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
    setAtBottom(bottom);
    if (bottom) setUnseen(0);
  };
  return (
    <div className="relative flex-1 min-h-0 flex flex-col">
      {pending.length > 0 && (
        <div className="sticky top-0 z-sticky p-2 flex flex-col gap-2 bg-canvas/95 border-b border-border">
          {pending.map((a) => a.kind === "approval" && <ApprovalCard key={a.id} runId={runId} a={a} />)}
        </div>
      )}
      <div ref={ref} onScroll={onScroll} role="feed" aria-label="Run timeline" aria-busy={status === "running"}
        className="flex-1 min-h-0 overflow-auto p-3 flex flex-col gap-2 scroll-pb-24">
        {shown.length === 0 && <EmptyState title={live ? "Waiting for the council…" : "No events"} />}
        {shown.map((it, i) => (
          <article key={it.id} id={`run-item-${it.id}`} aria-posinset={i + 1} aria-setsize={shown.length} aria-label={`${it.kind} ${"actor" in it ? it.actor?.name ?? "" : ""}`}
            className={cn("animate-enter rounded-md transition-shadow", flash === it.id && "ring-2 ring-accent")}>
            {it.kind === "plan" && plan ? <PlanCard plan={plan} members={members} />
              : it.kind === "thought" ? <ThoughtCard runId={runId} t={it} live={live} selected={selected === it.id} onSelect={() => onSelect(it.id)} />
              : it.kind === "tool" ? <ToolChip t={it} onSelect={() => onSelect(it.id)} />
              : it.kind === "approval" ? <ApprovalCard runId={runId} a={it} />
              : it.kind === "vote" ? <VoteCard v={it} members={members} />
              : <SimpleRow item={it} onOpenArtifact={() => onSelect(`artifact:${(it.data as { path?: string }).path}`)} />}
          </article>
        ))}
      </div>
      {!atBottom && unseen > 0 && (
        <button onClick={() => { ref.current?.scrollTo({ top: ref.current.scrollHeight }); setUnseen(0); }}
          className="absolute bottom-3 left-1/2 -translate-x-1/2 rounded-full bg-accent text-accent-fg px-3 h-8 text-sm shadow-2 inline-flex items-center gap-1">
          <ArrowDown className="size-4" aria-hidden /> {unseen} new
        </button>
      )}
    </div>
  );
}

function ArtifactViewer({ runId, path }: { runId: string; path: string }) {
  const [content, setContent] = useState<string | null>(null);
  const [err, setErr] = useState<unknown>(null);
  const binary = /\.(pdf|docx)$/i.test(path);
  useEffect(() => {
    if (binary) return;
    setContent(null);
    api.get<string>(`${V1}/runs/${runId}/artifacts/content?path=${encodeURIComponent(path)}`).then(setContent, setErr);
  }, [runId, path, binary]);
  const download = async () => {
    const resp = await fetch(`${V1}/runs/${runId}/artifacts/download?path=${encodeURIComponent(path)}`, {
      headers: { Authorization: `Bearer ${auth.token ?? ""}` },
    });
    if (!resp.ok) return toast({ tone: "danger", title: "Download failed" });
    const url = URL.createObjectURL(await resp.blob());
    const a = document.createElement("a");
    a.href = url;
    a.download = path.split("/").pop() ?? "artifact";
    a.click();
    URL.revokeObjectURL(url);
  };
  return (
    <div className="flex flex-col gap-2 p-3 min-h-0">
      <div className="flex items-center gap-2">
        <span className="font-mono text-sm truncate flex-1">{path}</span>
        <Button size="sm" icon={Download} onClick={() => void download()}>Download</Button>
      </div>
      {binary ? <p className="text-sm text-fg-muted">Binary file. Download to view.</p>
        : err ? <ErrorState error={err} />
        : content == null ? <Skeleton className="h-40" />
        : path.endsWith(".md") ? <Markdown>{content}</Markdown>
        : <pre tabIndex={0} role="region" aria-label={`Contents of ${path}`} className="font-mono text-xs whitespace-pre overflow-auto bg-surface-2 rounded-md p-3 max-h-[70vh]">{content}</pre>}
    </div>
  );
}

function Inspector({ runId, selected }: { runId: string; selected: string | null }) {
  const [tab, setTab] = useState("detail");
  const item = useRun(runId, (s) => (selected && !selected.startsWith("artifact:") ? s.items[selected] : undefined));
  const tools = useRun(runId, (s) => s.order.map((id) => s.items[id]).filter((i) => i?.kind === "tool"));
  const sources = useRun(runId, (s) => s.sources);
  const artifacts = useArtifacts(runId);
  const [path, setPath] = useState<string | null>(null);
  useEffect(() => {
    if (selected?.startsWith("artifact:")) {
      setPath(selected.slice(9));
      setTab("artifacts");
    } else if (selected) setTab("detail");
  }, [selected]);
  return (
    <aside aria-label="Inspector" className="h-full flex flex-col min-h-0 bg-surface">
      <Tabs value={tab} onValueChange={setTab} className="flex-1" tabs={[
        { value: "detail", label: "Detail", content: item ? (
          <div className="p-3 text-sm flex flex-col gap-2">
            <p className="font-semibold">{"actor" in item ? item.actor?.name : item.kind}</p>
            <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs">
              <dt className="text-fg-subtle">Type</dt><dd>{item.kind}</dd>
              <dt className="text-fg-subtle">Seq</dt><dd className="tabular">{item.seq}</dd>
              <dt className="text-fg-subtle">Time</dt><dd>{clock(item.ts)}</dd>
              {item.kind === "thought" && <><dt className="text-fg-subtle">Model</dt><dd className="font-mono">{item.model ?? "—"}</dd>
                <dt className="text-fg-subtle">Tokens</dt><dd className="tabular">{item.tokensIn ?? 0} in / {item.tokensOut ?? 0} out</dd>
                <dt className="text-fg-subtle">Cost</dt><dd className="tabular">{usd(item.costUsd)}</dd>
                <dt className="text-fg-subtle">Latency</dt><dd className="tabular">{item.latencyMs ?? "—"} ms</dd></>}
            </dl>
            {item.kind === "thought" && <Markdown>{item.text}</Markdown>}
            {item.kind === "tool" && <pre tabIndex={0} role="region" aria-label="Item data" className="font-mono text-xs whitespace-pre-wrap">{JSON.stringify({ args: item.args, result: item.preview }, null, 2)}</pre>}
          </div>
        ) : <EmptyState title="Select an item" body="Pick a thought, tool call or artifact in the timeline." /> },
        { value: "tools", label: `Tools (${tools.length})`, content: (
          <ul className="p-2 flex flex-col gap-1 text-sm">
            {tools.map((t) => t?.kind === "tool" && (
              <li key={t.id} className="flex items-center gap-2 px-2 h-8 rounded hover:bg-surface-2">
                <span className="font-mono truncate flex-1">{t.tool}</span>
                <Badge tone={t.status === "ok" ? "success" : t.status === "running" ? "info" : "danger"}>{t.status}</Badge>
                <span className="text-xs tabular text-fg-subtle w-14 text-right">{t.durationMs ?? "—"}ms</span>
              </li>
            ))}
            {!tools.length && <EmptyState title="No tool calls yet" />}
          </ul>
        ) },
        { value: "artifacts", label: "Artifacts", content: path ? (
          <div className="flex flex-col min-h-0">
            <Button size="sm" variant="ghost" className="self-start m-2" icon={FolderTree} onClick={() => setPath(null)}>All files</Button>
            <ArtifactViewer runId={runId} path={path} />
          </div>
        ) : (
          <ul className="p-2 text-sm">
            {artifacts.data?.map((a) => (
              <li key={a.path}>
                <button className="w-full flex items-center gap-2 px-2 h-8 rounded hover:bg-surface-2 text-left" onClick={() => setPath(a.path)}>
                  <FileText className="size-4 text-fg-subtle" aria-hidden /><span className="font-mono truncate flex-1">{a.path}</span>
                  <span className="text-xs text-fg-subtle">v{a.version}</span>
                </button>
              </li>
            ))}
            {!artifacts.data?.length && <EmptyState title="No files yet" />}
          </ul>
        ) },
        { value: "sources", label: `Sources (${sources.length})`, content: (
          <ol className="p-3 flex flex-col gap-3 text-sm">
            {sources.map((s) => (
              <li key={s.source_id}>
                <p className="font-medium">[{s.n}] {s.title}</p>
                <a href={s.url} target="_blank" rel="noopener noreferrer nofollow" className="text-xs text-accent underline break-all">{s.url}</a>
                {s.snippet && <p className="text-xs text-fg-muted mt-1 line-clamp-3">{s.snippet}</p>}
              </li>
            ))}
            {!sources.length && <EmptyState title="No sources yet" body="Sources appear as agents fetch web pages." />}
          </ol>
        ) },
      ]} />
    </aside>
  );
}

const EXPORT_FORMATS: { value: OutputFormat; label: string }[] = [
  { value: "md", label: "Markdown" }, { value: "pdf", label: "PDF" }, { value: "latex", label: "LaTeX" },
  { value: "html", label: "HTML" }, { value: "docx", label: "Word" }, { value: "txt", label: "Text" }, { value: "json", label: "JSON" },
];

function OutputView({ runId, run }: { runId: string; run: RunDetail }) {
  const artifacts = useArtifacts(runId);
  const exports = useRun(runId, (s) => s.exports);
  const [busy, setBusy] = useState<string | null>(null);
  const qc = useQueryClient();
  const make = async (fmt: OutputFormat) => {
    setBusy(fmt);
    try {
      await api.post(`${V1}/runs/${runId}/exports`, { format: fmt });
      await qc.invalidateQueries({ queryKey: keys.artifacts(runId) });
      toast({ tone: "success", title: `${fmt.toUpperCase()} export ready` });
    } catch (err) {
      toast({ tone: "danger", title: "Export failed", body: errorMessage(err) });
    } finally {
      setBusy(null);
    }
  };
  const files = artifacts.data ?? [];
  const exportsList = files.filter((f) => f.path.startsWith("exports/"));
  const workspace = files.filter((f) => !f.path.startsWith("exports/"));
  const [selected, setSelected] = useState<string | null>(null);
  return (
    <div className="flex-1 min-h-0 overflow-auto p-4 md:p-6 grid gap-6 lg:grid-cols-[1fr_280px]">
      <div className="min-w-0">
        {selected ? <ArtifactViewer runId={runId} path={selected} /> : run.result ? (
          <article className="max-w-3xl"><Markdown>{run.result}</Markdown></article>
        ) : (
          <EmptyState icon={FileText} title="No deliverable yet" body="The deliverable appears when the council finishes." />
        )}
      </div>
      <aside className="flex flex-col gap-4">
        <section>
          <h2 className="text-sm font-semibold mb-2">Export</h2>
          <div className="flex flex-wrap gap-1.5">
            {EXPORT_FORMATS.map((f) => (
              <Button key={f.value} size="sm" loading={busy === f.value} disabled={!run.result} onClick={() => void make(f.value)}>
                {f.label}{exports[f.value]?.status === "failed" ? " ⚠" : ""}
              </Button>
            ))}
          </div>
          {exportsList.length > 0 && (
            <ul className="mt-2 text-sm">{exportsList.map((f) => (
              <li key={f.path}><button className="text-accent underline font-mono text-xs" onClick={() => setSelected(f.path)}>{f.path}</button></li>
            ))}</ul>
          )}
        </section>
        <section>
          <h2 className="text-sm font-semibold mb-2">Workspace</h2>
          {selected && <Button size="sm" variant="ghost" onClick={() => setSelected(null)}>Show deliverable</Button>}
          <ul className="text-sm">
            {workspace.map((f) => (
              <li key={f.path}><button className="w-full text-left font-mono text-xs px-1 py-1 rounded hover:bg-surface-2 truncate" onClick={() => setSelected(f.path)}>{f.path}</button></li>
            ))}
            {!workspace.length && <li className="text-fg-subtle text-xs">No files</li>}
          </ul>
        </section>
      </aside>
    </div>
  );
}

export default function RunWorkspace() {
  const { runId = "" } = useParams();
  const loc = useLocation();
  const detail = useRunDetail(runId);
  useRunStream(runId, detail.data);
  // While replaying, the run views read the separate replay state; controls keep using the real run.
  const viewId = useReplayView(runId);
  const members = useRun(viewId, (s) => s.members);
  const state = useRun(viewId, (s) => s);
  const realStatus = useRun(runId, (s) => (s.lastSeq ? s.status : ""));
  const [density, setDensity] = useState<Density>("working");
  const [focus, setFocus] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [view, setViewState] = useState<RunView>(loadView);
  const [reveal, setReveal] = useState<string | null>(null);
  const setView = useCallback((v: RunView) => {
    setViewState(v);
    saveView(v);
  }, []);
  const onVizSelect = useCallback<VizSelect>(({ itemId, nodeId }) => {
    const st = useRuns.getState().runs[viewId];
    setView("timeline");
    if (itemId && st?.items[itemId]) {
      const it = st.items[itemId];
      if (!visible(it, density)) setDensity("everything");
      if (focus && !("actor" in it && it.actor?.member_id === focus)) setFocus(null);
      setSelected(itemId);
      setReveal(itemId);
    } else if (nodeId && st?.members.some((m) => m.id === nodeId)) {
      setFocus(nodeId);
    }
  }, [viewId, density, focus, setView]);
  const pendingCount = Object.values(state.items).filter((i) => i.kind === "approval" && i.status === "pending").length;
  useEffect(() => {
    if (!detail.data) return;
    document.title = `${pendingCount ? `(${pendingCount}) ` : ""}${detail.data.topic} · Argus`;
    return () => { document.title = "Argus Council"; };
  }, [detail.data, pendingCount]);

  if (detail.isLoading) {
    return <div className="p-4 grid gap-3"><Skeleton className="h-16" /><div className="grid grid-cols-[260px_1fr] gap-3"><Skeleton className="h-96" /><Skeleton className="h-96" /></div></div>;
  }
  if (detail.error || !detail.data) {
    return <div className="p-6"><ErrorState error={detail.error} onRetry={() => void detail.refetch()} /><p className="text-center"><Link to="/runs" className="text-accent underline">All runs</Link></p></div>;
  }
  const run = detail.data;
  const live = !isTerminal(realStatus || run.status);
  const onOutput = loc.pathname.endsWith("/output");
  return (
    <div className="flex flex-col h-[calc(100vh-3rem)] md:h-[calc(100vh-3rem)]">
      <Header run={run} runId={runId} />
      <Banner run={run} runId={runId} />
      {onOutput ? <OutputView runId={runId} run={run} /> : (
        <>
          {!live && <ReplayBar runId={runId} members={run.council_snapshot.members ?? []} />}
          <div className="flex-1 min-h-0 grid grid-cols-1 md:grid-cols-[240px_1fr] xl:grid-cols-[260px_1fr_380px]">
            <div className="hidden md:block border-r border-border min-h-0 overflow-auto">
              <CouncilRail runId={runId} members={members.length ? members : run.council_snapshot.members} state={state} live={live}
                focused={focus} onFocus={setFocus} />
            </div>
            <div className="flex flex-col min-h-0 min-w-0">
              <div className="flex flex-wrap items-center gap-2 px-3 py-2 border-b border-border bg-surface">
                <SegmentedTabs label="Run visualisation" value={view} onChange={setView} options={VIEWS} idPrefix="run-view" panelId="run-view-panel" />
                {view === "timeline" && <>
                <label htmlFor="density" className="text-xs text-fg-subtle">Show</label>
                <select id="density" value={density} onChange={(e) => setDensity(e.target.value as Density)}
                  className="h-8 rounded-md border border-border-strong bg-surface px-2 text-sm">
                  <option value="decisions">Decisions</option>
                  <option value="working">Working</option>
                  <option value="everything">Everything</option>
                </select>
                {focus && <Button size="sm" variant="ghost" onClick={() => setFocus(null)}>Clear focus</Button>}
                </>}
                <span className="ml-auto text-xs text-fg-subtle tabular">{state.order.length} events · seq {state.lastSeq}</span>
              </div>
              <div id="run-view-panel" role="tabpanel" aria-labelledby={`run-view-tab-${view}`} className="flex-1 min-h-0 flex flex-col">
                {view === "timeline" ? (
                  <Timeline runId={viewId} density={density} focus={focus} onSelect={setSelected} selected={selected}
                    reveal={reveal} onRevealed={() => setReveal(null)} />
                ) : (() => {
                  const View = VIEWS.find((v) => v.value === view)?.component;
                  return View ? (
                    <VizBoundary key={view}>
                      <Suspense fallback={<div className="p-4"><Skeleton className="h-96" /></div>}>
                        <div className="flex-1 min-h-0 overflow-auto"><View run={state} onSelect={onVizSelect} /></div>
                      </Suspense>
                    </VizBoundary>
                  ) : null;
                })()}
              </div>
            </div>
            <div className="hidden xl:flex border-l border-border min-h-0 flex-col">
              <Inspector runId={runId} selected={selected} />
            </div>
          </div>
          <InterventionBar runId={runId} members={members.length ? members : run.council_snapshot.members} disabled={!live} />
        </>
      )}
      {run.status === "queued" && !state.lastSeq && <Callout tone="info" title="Waiting to start…" />}
    </div>
  );
}
