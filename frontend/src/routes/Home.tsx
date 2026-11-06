import { ArrowUpRight, BookPlus, ChevronDown, ChevronRight, History, Play, SlidersHorizontal, Sparkles } from "lucide-react";
import { useEffect, useMemo, useState, type FormEvent, type ReactNode } from "react";
import { Link, useLocation, useNavigate } from "react-router";

import { RoleChip } from "@/components/councils/parts";
import { StatusDot } from "@/components/insights/charts";
import { KnowledgePicker, QuickAddKnowledgeDialog } from "@/components/knowledge/AddKnowledge";
import { CouncilPlanPreview } from "@/components/run/CouncilPlanPreview";
import { RunRowList } from "@/components/run/RunRow";
import {
  Button, Callout, Card, Chip, EmptyState, ErrorState, Kbd, Page, PageHeader, SectionTitle, Segmented, SelectField, Skeleton,
  Stepper, TextArea, TextField, Toggle, touchTarget,
} from "@/components/ui";
import { MODES } from "@/features/councils/logic";
import { fmtNum, fmtUsd } from "@/features/insights/logic";
import { useInsights, useProviderHealth } from "@/features/insights/queries";
import { useKnowledgeBases } from "@/features/knowledge/queries";
import { SaveTemplateButton, TemplatePicker } from "@/features/templates/components";
import { errorMessage } from "@/lib/api";
import { cn } from "@/lib/cn";
import { providerKindLabel } from "@/lib/providers";
import {
  useCouncils, useCreateRun, usePlanCouncil, useProviderKinds, useProviders, useRoles, useRunsList, type CouncilPlan, type ModelStrategy,
  type PlanRequest, type ReportDepth,
} from "@/lib/queries";
import type { Mode, OutputFormat } from "@/lib/types";

const DEFAULT_SIZE: Record<Mode, number> = { research: 5, coding: 6, chat: 2, general: 4 };
const FORMATS: { value: OutputFormat; label: string }[] = [
  { value: "md", label: "Markdown" }, { value: "pdf", label: "PDF" }, { value: "latex", label: "LaTeX" },
  { value: "html", label: "HTML" }, { value: "docx", label: "Word" }, { value: "txt", label: "Text" },
];
const PRESETS = {
  strict: { budget: { usd: 1 }, max_steps: 40, oversight: { intercept_mode: "hold", monitor_sensitivity: "high" } },
  balanced: {},
  permissive: { budget: { usd: 20 }, max_steps: 200, oversight: { intercept_mode: "annotate", monitor_sensitivity: "low" } },
} as const;
const STRATEGIES: { value: ModelStrategy; label: string; hint: string }[] = [
  { value: "quality", label: "Quality", hint: "The most competent model for every seat; cost counts little." },
  { value: "balanced", label: "Balanced", hint: "Competence for each seat's job, weighed against what it will cost." },
  { value: "economy", label: "Economy", hint: "The cheapest model that is still competent for each seat." },
];
const DEPTHS: { value: ReportDepth; label: string }[] = [
  { value: "brief", label: "Brief" }, { value: "standard", label: "Standard" }, { value: "extensive", label: "Extensive" },
];
const APPROVALS = [
  { key: "shell", label: "Shell commands" },
  { key: "network_write", label: "Network writes" },
  { key: "remote_agent", label: "Delegating to remote agents" },
  { key: "package_install", label: "Installing packages" },
];
const EXAMPLES = [
  { topic: "State of solid-state batteries in 2026", mode: "research" as Mode },
  { topic: "Build a CLI that converts CSV files to JSON", mode: "coding" as Mode },
  { topic: "Explain Rust ownership with examples", mode: "chat" as Mode },
];

const AUTO_MODE_KEY = "argus.autoMode";
const AUTO_EXTEND_KEY = "argus.autoExtend";
const BUFFER_KEY = "argus.bufferUsd";
const readValue = (key: string) => {
  try {
    return localStorage.getItem(key) ?? "";
  } catch {
    return "";
  }
};
const writeValue = (key: string, value: string) => {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* ignore */
  }
};
const readFlag = (key: string) => {
  try {
    return localStorage.getItem(key) === "on";
  } catch {
    return false;
  }
};
const writeFlag = (key: string, on: boolean) => {
  try {
    localStorage.setItem(key, on ? "on" : "off");
  } catch {
    /* ignore */
  }
};

/** Mirrors backend roster_for(mode, size). */
export function rosterFor(rosters: Record<string, string[]> | undefined, overseers: Set<string>, mode: Mode, size: number): string[] {
  const base = rosters?.[mode] ?? rosters?.general ?? [];
  if (size <= base.length) return base.slice(0, size);
  const workers = base.filter((r) => !overseers.has(r) && r !== "chair");
  const pool = workers.length ? workers : ["assistant"];
  return [...base, ...Array.from({ length: size - base.length }, (_, i) => pool[i % pool.length]!)];
}

/** Router state a "Start a follow-up run" link passes to prefill the composer. */
export interface ComposerPrefill { topic: string; mode: Mode; description?: string }
const MIN_SEATS = 1;
const MAX_SEATS = 12;
/** Select value for "let the server decide" (council roles / per-seat models). */
const AUTO = "__auto__";
const AUTO_MAX_SEATS = 8;

function Composer() {
  const navigate = useNavigate();
  const location = useLocation();
  const prefill = (location.state as { prefill?: ComposerPrefill } | null)?.prefill;
  const roles = useRoles();
  const councils = useCouncils();
  const kinds = useProviderKinds();
  const providers = useProviders();
  const create = useCreateRun();
  const [topic, setTopic] = useState(prefill?.topic ?? "");
  const [description, setDescription] = useState(prefill?.description ?? "");
  const [mode, setMode] = useState<Mode>(prefill?.mode ?? "research");
  // AUTO: compose the council's roles for this task (council) / pick a model for every seat (model).
  const [councilId, setCouncilId] = useState(AUTO);
  const [size, setSize] = useState(DEFAULT_SIZE.research);
  const [outputs, setOutputs] = useState<OutputFormat[]>(["md"]);
  const [preset, setPreset] = useState<keyof typeof PRESETS>("balanced");
  const [advanced, setAdvanced] = useState(false);
  const [budget, setBudget] = useState("5");
  const [maxSteps, setMaxSteps] = useState("80");
  const [interceptMode, setInterceptMode] = useState("hold");
  const [approvals, setApprovals] = useState<string[]>(["shell", "package_install", "remote_agent", "network_write"]);
  const [route, setRoute] = useState(AUTO);
  const [strategy, setStrategy] = useState<ModelStrategy>("balanced");
  const [depth, setDepth] = useState<ReportDepth>("extensive");
  const [checkins, setCheckins] = useState(false);
  // Auto mode and automatic limit raises are remembered between runs on this browser.
  const [autoMode, setAutoMode] = useState(() => readFlag(AUTO_MODE_KEY));
  const [autoExtend, setAutoExtend] = useState(() => readFlag(AUTO_EXTEND_KEY));
  useEffect(() => writeFlag(AUTO_MODE_KEY, autoMode), [autoMode]);
  useEffect(() => writeFlag(AUTO_EXTEND_KEY, autoExtend), [autoExtend]);
  const [bufferUsd, setBufferUsd] = useState(() => readValue(BUFFER_KEY));
  useEffect(() => writeValue(BUFFER_KEY, bufferUsd), [bufferUsd]);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const planner = usePlanCouncil();
  const [plan, setPlan] = useState<{ key: string; plan: CouncilPlan } | null>(null);
  const knowledge = useKnowledgeBases();
  const [kbIds, setKbIds] = useState<string[]>([]);
  const [addingKnowledge, setAddingKnowledge] = useState(false);

  const autoCouncil = councilId === AUTO;
  const adHoc = !councilId || autoCouncil;
  const autoModels = adHoc && route === AUTO;
  const reports = mode === "research" || mode === "general";
  /** The run's dollar budget, which the planned council is fitted to. */
  const budgetUsd = advanced ? Number(budget) || 5 : ((PRESETS[preset] as { budget?: { usd: number } }).budget?.usd ?? 5);
  useEffect(() => setSize(autoCouncil ? AUTO_MAX_SEATS : DEFAULT_SIZE[mode]), [mode, autoCouncil]);
  useEffect(() => {
    const prefill = (e: Event) => {
      const d = (e as CustomEvent<{ topic: string; mode: Mode }>).detail;
      setTopic(d.topic);
      setMode(d.mode);
    };
    window.addEventListener("argus:prefill", prefill);
    return () => window.removeEventListener("argus:prefill", prefill);
  }, []);

  const overseers = useMemo(() => new Set(roles.data?.roles.filter((r) => r.overseer).map((r) => r.key)), [roles.data]);
  const roster = rosterFor(roles.data?.rosters, overseers, mode, size);

  const routes = useMemo(() => {
    const out: { value: string; label: string }[] = [];
    for (const p of providers.data ?? []) {
      if (!p.enabled) continue;
      const kind = kinds.data?.kinds.find((k) => k.key === p.kind);
      const models = [...new Set([...(p.default_model ? [p.default_model] : []), ...(kind?.suggested_models ?? [])])];
      for (const m of models) out.push({ value: `${p.name}::${m}`, label: `${p.name} · ${m}` });
    }
    for (const sys of kinds.data?.system_providers ?? []) {
      const kind = kinds.data?.kinds.find((k) => k.key === sys);
      for (const m of kind?.suggested_models.length ? kind.suggested_models : sys === "mock" ? ["mock-1"] : [])
        out.push({ value: `${sys}::${m}`, label: `${sys === "mock" ? providerKindLabel(sys) : sys} · ${m}` });
    }
    return out;
  }, [providers.data, kinds.data]);

  const noProviders = !providers.isLoading && !kinds.isLoading && routes.every((r) => r.value.startsWith("mock::"));
  // Keep the selection on a route that exists (or Auto).
  useEffect(() => {
    if (route !== AUTO && routes.length && !routes.some((r) => r.value === route)) setRoute(AUTO);
  }, [routes, route]);

  /** What the server is asked to plan; a preview is only reused while this stays the same. */
  const planRequest = (): PlanRequest | null => {
    if (!adHoc || (!autoCouncil && !autoModels) || !topic.trim()) return null;
    const [provider, model] = route === AUTO ? [undefined, undefined] : route.split("::");
    return {
      topic: topic.trim(), description, mode, max_size: size, provider, model,
      auto_council: autoCouncil, auto_models: autoModels, model_strategy: strategy, budget_usd: budgetUsd,
      ...(autoCouncil ? {} : { roles: roster }),
    };
  };
  const request = planRequest();
  const planKey = request ? JSON.stringify(request) : "";
  const freshPlan = plan && plan.key === planKey ? plan.plan : null;
  const preview = () => {
    if (!request) return;
    planner.mutate(request, { onSuccess: (p) => setPlan({ key: JSON.stringify(request), plan: p }) });
  };

  /** The composer's run configuration (also what "Save as template" stores). */
  const buildConfig = () => {
    const [provider, model] = route.split("::");
    const guardrails: Record<string, unknown> = { ...PRESETS[preset] };
    if (advanced) {
      guardrails.budget = { usd: Number(budget) || 5 };
      guardrails.max_steps = Number(maxSteps) || 80;
      guardrails.oversight = { intercept_mode: interceptMode };
      guardrails.approval_required = approvals;
    }
    if (reports) guardrails.report = { depth, human_checkins: checkins };
    if (autoMode) guardrails.auto_mode = true;
    if (autoExtend) guardrails.extensions = { auto_approve: true };
    if (Number(bufferUsd) > 0) {
      guardrails.budget = { ...(guardrails.budget as Record<string, unknown> | undefined), buffer_usd: Number(bufferUsd) };
    }
    const council = !adHoc
      ? { council_id: councilId }
      : {
          size, ...(autoModels ? { model_strategy: strategy } : { provider, model }),
          auto_council: autoCouncil, auto_models: autoModels,
        };
    return { topic: topic.trim(), description, mode, outputs, ...council, guardrails };
  };

  const submit = async (e?: FormEvent) => {
    e?.preventDefault();
    if (!topic.trim()) return;
    try {
      // A fresh preview is started exactly as shown; otherwise the server plans when the run is created.
      const seats = freshPlan?.seats.map(({ role, name, focus, provider, model }) => ({ role, name, focus, provider, model }));
      const run = await create.mutateAsync({
        ...buildConfig(),
        ...(seats ? { seats } : {}),
        ...(kbIds.length ? { knowledge_base_ids: kbIds } : {}),
      });
      navigate(`/runs/${run.id}`);
    } catch {
      /* rendered below */
    }
  };

  const councilName = autoCouncil ? "Auto council" : !councilId ? "Default roster"
    : councils.data?.find((c) => c.id === councilId)?.name ?? "Saved council";
  const modelName = councilId ? null : autoModels ? `${STRATEGIES.find((x) => x.value === strategy)?.label} models`
    : routes.find((r) => r.value === route)?.label ?? route.split("::")[1];
  const summary = [
    councilName, modelName, outputs.map((o) => FORMATS.find((f) => f.value === o)?.label).join(", ") || "No files",
    `${preset[0]!.toUpperCase()}${preset.slice(1)} guardrails`, kbIds.length ? `${kbIds.length} knowledge` : null,
    autoMode ? "Auto mode" : null,
    Number(bufferUsd) > 0 ? `$${Number(bufferUsd)} reserve` : null,
  ].filter(Boolean).join(" · ");

  return (
    // ⌘↵ submits from any field in the composer.
    // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions
    <form onSubmit={submit} className="flex flex-col gap-3" aria-label="New run"
      onKeyDown={(e) => { if ((e.metaKey || e.ctrlKey) && e.key === "Enter") void submit(); }}>
      <Card as="div" className="overflow-hidden">
        {/* The task itself: Topic, Description and Mode describe one thing, so they sit tight together. */}
        <div className="flex flex-col gap-4 p-5">
          <TextField label="Topic" required maxLength={200} autoFocus placeholder="A question, a build, a decision to research"
            value={topic} onChange={(e) => setTopic(e.target.value)} />
          <TextArea label="Description" maxLength={4000} placeholder="Details, constraints, what a great result looks like (Markdown)"
            hint={`${description.length}/4000`} value={description} onChange={(e) => setDescription(e.target.value)} />
          <div>
            <Segmented label="Mode" value={mode} onChange={setMode} options={MODES} />
            <p className="text-xs text-fg-subtle mt-1.5">{MODES.find((m) => m.value === mode)?.hint}</p>
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-x-3 gap-y-2 border-t border-border bg-surface-2/50 px-5 py-3">
          <button type="button" onClick={() => setSettingsOpen((o) => !o)} aria-expanded={settingsOpen} aria-controls="run-settings"
            className={cn("group -ml-2 flex min-h-9 w-[calc(100%+0.5rem)] min-w-0 items-center gap-2 rounded-md px-2 text-left transition-colors duration-fast sm:w-auto sm:flex-1",
              "hover:bg-surface-3/60", touchTarget)}>
            <SlidersHorizontal className="size-4 shrink-0 text-fg-muted" aria-hidden />
            <span className="text-sm font-medium shrink-0">Run settings</span>
            <span className="truncate text-xs text-fg-subtle">{summary}</span>
            <ChevronDown className={cn("ml-auto size-4 shrink-0 text-fg-muted transition-transform duration-fast", settingsOpen && "rotate-180")} aria-hidden />
          </button>
          <div className="flex items-center gap-2 ml-auto">
            <span className="text-xs text-fg-subtle [@media(pointer:coarse)]:hidden">
              {topic.trim() ? <><Kbd>⌘</Kbd> <Kbd>↵</Kbd></> : <span id="start-hint">Add a topic to start.</span>}
            </span>
            <SaveTemplateButton getConfig={buildConfig} disabled={!topic.trim()} />
            <Button type="submit" variant="primary" icon={Play} loading={create.isPending} disabled={!topic.trim() || (!councilId && !route)}
              aria-describedby={!topic.trim() ? "start-hint" : undefined}>
              Start run
            </Button>
          </div>
        </div>

        {settingsOpen && (
          <div id="run-settings" className="flex flex-col gap-8 border-t border-border p-5">
            <div className="flex flex-col gap-4">
              <div className="grid gap-4 sm:grid-cols-2">
                <SelectField label="Council" value={councilId} onChange={(e) => setCouncilId(e.target.value)}
                  hint={autoCouncil ? "Roles and their focus are chosen for this task." : undefined}>
                  <option value={AUTO}>Auto: compose for this task</option>
                  <option value="">Default roster for the mode</option>
                  {councils.data?.map((c) => <option key={c.id} value={c.id}>{c.name} ({c.members.length} seats)</option>)}
                </SelectField>
                {!councilId && (
                  <SelectField label="Model" value={route} onChange={(e) => setRoute(e.target.value)}
                    hint={noProviders ? "No provider connected yet."
                      : autoModels ? "Each seat gets the model most competent for its job, weighed against its cost and your budget." : undefined}>
                    <option value={AUTO}>Auto: best model per seat</option>
                    {routes.map((r) => <option key={r.value} value={r.value}>{r.label}</option>)}
                  </SelectField>
                )}
              </div>

              {autoModels && (
                <div className="flex flex-col gap-1">
                  <Segmented label="Model choice" value={strategy} onChange={setStrategy}
                    options={STRATEGIES.map(({ value, label }) => ({ value, label }))} />
                  <p className="text-xs text-fg-subtle">{STRATEGIES.find((s) => s.value === strategy)?.hint} Budget ${budgetUsd}.</p>
                </div>
              )}

              {!councilId && (
                <div className="flex flex-col gap-2">
                  <Stepper label={autoCouncil ? "Max seats" : "Council size"} value={size} min={MIN_SEATS} max={MAX_SEATS} onChange={setSize}
                    hint={autoCouncil ? "The planner uses as few seats as the task needs, up to this." : `Between ${MIN_SEATS} and ${MAX_SEATS} seats.`} />
                  {!autoCouncil && !freshPlan && (
                    <ul className="flex flex-wrap gap-1.5" aria-label="Roster preview">
                      {roster.map((r, i) => <RoleChip key={`${r}-${i}`} role={r} roles={roles.data?.roles} overseer={overseers.has(r)} />)}
                    </ul>
                  )}
                  {(autoCouncil || autoModels) && (
                    <div className="flex flex-col gap-2">
                      <Button type="button" size="sm" variant="ghost" className="self-start -ml-2.5" icon={Sparkles}
                        loading={planner.isPending} disabled={!request} onClick={preview}
                        title={!request ? "Add a topic first" : undefined}>
                        {freshPlan ? "Plan again" : autoCouncil ? "Preview the council" : "Preview the models"}
                      </Button>
                      {planner.error != null && <Callout tone="danger" title="Could not plan the council">{errorMessage(planner.error)}</Callout>}
                      {plan && <CouncilPlanPreview plan={plan.plan} stale={!freshPlan} />}
                      {!plan && autoCouncil && (
                        <p className="text-xs text-fg-subtle">The council is composed when the run starts. Preview it to see the seats and models first.</p>
                      )}
                    </div>
                  )}
                </div>
              )}
            </div>

            <div className="flex flex-col gap-2">
              <KnowledgePicker legend="Knowledge to link" bases={knowledge.data ?? []} value={kbIds} onChange={setKbIds} />
              <Button type="button" variant="ghost" size="sm" className="self-start -ml-2.5" icon={BookPlus} onClick={() => setAddingKnowledge(true)}>
                Add a note or link…
              </Button>
              {addingKnowledge && (
                <QuickAddKnowledgeDialog open onOpenChange={setAddingKnowledge} bases={knowledge.data ?? []}
                  defaultName={topic.trim() ? `Notes: ${topic.trim()}`.slice(0, 120) : "Run notes"}
                  onAdded={(id) => setKbIds((ids) => (ids.includes(id) ? ids : [...ids, id]))} />
              )}
            </div>

            <div className="flex flex-col gap-4">
              <fieldset>
                <legend className="text-sm font-medium mb-2">Deliverable formats</legend>
                <div className="flex flex-wrap gap-2">
                  {FORMATS.map((f) => {
                    const on = outputs.includes(f.value);
                    return (
                      <Chip key={f.value} pressed={on}
                        onPressedChange={(next) => setOutputs((o) => (next ? [...o, f.value] : o.filter((x) => x !== f.value)))}>
                        {f.label}
                      </Chip>
                    );
                  })}
                </div>
              </fieldset>

              {reports && (
                <div className="flex flex-col gap-3 rounded-lg border border-border p-3">
                  <Segmented label="Report depth" value={depth} onChange={setDepth} options={DEPTHS} />
                  <p className="text-xs text-fg-subtle -mt-2">
                    Specialist agents outline, chart, draft and review the report in rounds until it passes its quality checks.
                  </p>
                  <Toggle label="Let me check the outline" checked={checkins} onChange={setCheckins} disabled={autoMode}
                    hint={autoMode ? "Auto mode approves the outline for you." : "The run pauses once the outline is ready so you can approve it or send it back with notes."} />
                </div>
              )}
            </div>

            <div className="flex flex-col gap-4">
              <div className="flex flex-col gap-2">
                <SelectField label="Guardrails" value={preset} onChange={(e) => setPreset(e.target.value as keyof typeof PRESETS)}
                  hint="Budget, step limits and how strongly overseers intervene." className="sm:max-w-[calc(50%-0.5rem)]">
                  <option value="strict">Strict</option>
                  <option value="balanced">Balanced (default)</option>
                  <option value="permissive">Permissive</option>
                </SelectField>
              </div>
              <TextField label="Budget buffer (USD)" type="number" min="0" step="0.5" inputMode="decimal" placeholder="0"
                value={bufferUsd} onChange={(e) => setBufferUsd(e.target.value)} className="sm:max-w-[calc(50%-0.5rem)]"
                hint="Extra spend the run draws on, without asking, when it reaches its budget, instead of halting." />
              <div className="grid gap-3 sm:grid-cols-2">
                <Toggle label="Auto mode" checked={autoMode} onChange={setAutoMode}
                  hint="Agents never stop to ask. Every approval is granted and logged; blocked actions stay blocked." />
                <Toggle label="Raise limits automatically" checked={autoExtend} onChange={setAutoExtend}
                  hint="When the run reaches its step, turn, runtime or token limit, it's raised without asking (up to the extension cap)." />
              </div>
              <Button type="button" variant="ghost" size="sm" className="self-start -ml-2.5" icon={advanced ? ChevronDown : ChevronRight}
                onClick={() => setAdvanced((a) => !a)} aria-expanded={advanced} aria-controls="advanced-guardrails">
                {advanced ? "Hide advanced guardrails" : "Advanced guardrails"}
              </Button>
              {advanced && (
                <div id="advanced-guardrails" className="grid gap-4 sm:grid-cols-3 rounded-lg bg-surface-2 p-4">
                  <TextField label="Budget (USD)" type="number" min="0" step="0.5" value={budget} onChange={(e) => setBudget(e.target.value)} />
                  <TextField label="Max steps" type="number" min="1" value={maxSteps} onChange={(e) => setMaxSteps(e.target.value)} />
                  <SelectField label="Overseer intercepts" value={interceptMode} onChange={(e) => setInterceptMode(e.target.value)}>
                    <option value="annotate">Annotate (comment only)</option>
                    <option value="hold">Hold (agent waits for correction)</option>
                    <option value="block">Block (discard step)</option>
                  </SelectField>
                  <fieldset className="sm:col-span-3">
                    <legend className="text-sm font-medium mb-2">Require my approval for</legend>
                    {autoMode && <p className="text-xs text-fg-subtle mb-2">Auto mode is on, so these are approved for you.</p>}
                    <div className="grid gap-3 sm:grid-cols-2">
                      {APPROVALS.map((a) => (
                        <Toggle key={a.key} label={a.label} checked={approvals.includes(a.key)} disabled={autoMode}
                          onChange={(on) => setApprovals((x) => (on ? [...x, a.key] : x.filter((k) => k !== a.key)))} />
                      ))}
                    </div>
                  </fieldset>
                </div>
              )}
            </div>
          </div>
        )}
      </Card>

      {create.error && <Callout tone="danger" title="Could not start the run">{errorMessage(create.error)}</Callout>}
      {noProviders && (
        <Callout tone="warning" title="No model provider connected"
          action={<Link to="/connections/providers" className="text-sm text-accent underline self-center">Connect a provider</Link>}>
          Connect Anthropic, OpenAI, Gemini, Ollama or another provider (or a CLI agent) before starting a run.
        </Callout>
      )}
    </form>
  );
}

// ------------------------------------------------------------------ At a glance
/** Tools at or above this error rate are called out on Home (the Insights "unreliable" band). */
const UNRELIABLE = 20;

function GlanceHeading({ id, children, to, linkLabel }: { id: string; children: ReactNode; to: string; linkLabel: string }) {
  return (
    <div className="flex items-baseline justify-between gap-2 mb-2">
      <h2 id={id} className="text-sm font-semibold">{children}</h2>
      <Link to={to} className="inline-flex items-center gap-0.5 text-xs text-fg-muted hover:text-accent min-h-6">
        {linkLabel}<ArrowUpRight className="size-3.5" aria-hidden />
      </Link>
    </div>
  );
}

function WeekGlance() {
  const q = useInsights(7);
  const t = q.data?.totals;
  const stats = t ? [
    { label: "Runs", value: fmtNum(t.runs) },
    { label: "Success", value: t.runs ? `${t.success_rate}%` : "—" },
    { label: "Spend", value: fmtUsd(t.cost_usd) },
    { label: "Tokens", value: fmtNum(t.tokens_in + t.tokens_out) },
  ] : [];
  return (
    <section aria-labelledby="week-h">
      <GlanceHeading id="week-h" to="/insights" linkLabel="Insights">Last 7 days</GlanceHeading>
      {q.isLoading ? <Skeleton className="h-28" /> : q.error ? (
        <p className="text-sm text-fg-muted">Couldn't load this week's numbers.</p>
      ) : (
        <dl className="grid grid-cols-2 gap-px overflow-hidden rounded-lg border border-border bg-border">
          {stats.map((s) => (
            <div key={s.label} className="bg-surface px-3 py-2.5">
              <dt className="text-xs text-fg-subtle">{s.label}</dt>
              <dd className="text-xl font-display tabular">{s.value}</dd>
            </div>
          ))}
        </dl>
      )}
    </section>
  );
}

function ToolHealth() {
  const q = useInsights(7);
  const tools = q.data?.tools ?? [];
  if (!tools.length) return null;
  const flaky = tools.filter((t) => t.error_rate >= UNRELIABLE).sort((a, b) => b.error_rate - a.error_rate);
  return (
    <section aria-labelledby="tools-h">
      <GlanceHeading id="tools-h" to="/insights" linkLabel="Reliability">Tool health</GlanceHeading>
      {flaky.length === 0 ? (
        <p className="flex items-center gap-2 text-sm text-fg-muted">
          <StatusDot tone="success" label="Healthy" />Every tool is under {UNRELIABLE}% errors this week.
        </p>
      ) : (
        <ul className="flex flex-col divide-y divide-border rounded-lg border border-border bg-surface">
          {flaky.slice(0, 5).map((t) => (
            <li key={t.tool} className="flex items-center gap-2.5 px-3 py-2 min-w-0">
              <StatusDot tone={t.error_rate >= 50 ? "danger" : "warning"} label="Unreliable" />
              <span className="font-mono text-sm truncate" title={t.tool}>{t.tool}</span>
              <span className="ml-auto shrink-0 text-xs tabular text-fg-muted">
                <span className="font-semibold text-fg">{t.error_rate}%</span> of {fmtNum(t.calls)}
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function ProviderGlance() {
  const q = useProviderHealth();
  const enabled = (q.data ?? []).filter((p) => p.enabled);
  if (q.isLoading || q.error) return null;
  const down = enabled.filter((p) => p.health_status && !/^(ok|up|healthy|pass)/i.test(p.health_status));
  return (
    <section aria-labelledby="providers-h">
      <GlanceHeading id="providers-h" to="/connections/providers" linkLabel="Connections">Models</GlanceHeading>
      <p className="flex items-center gap-2 text-sm text-fg-muted">
        <StatusDot tone={!enabled.length ? "warning" : down.length ? "danger" : "success"} label={down.length ? "Some providers down" : "Providers"} />
        {!enabled.length ? "No provider connected yet."
          : down.length ? `${down.length} of ${enabled.length} providers not answering: ${down.map((p) => p.name).join(", ")}.`
            : `${enabled.length} provider${enabled.length === 1 ? "" : "s"} connected.`}
      </p>
    </section>
  );
}

// ------------------------------------------------------------------ Page
const today = () => new Intl.DateTimeFormat(undefined, { weekday: "long", day: "numeric", month: "long" }).format(new Date());

export default function Home() {
  const running = useRunsList({ status: "running", limit: 20 });
  const paused = useRunsList({ status: "paused", limit: 20 });
  // Live runs with an approval request open come first; the rest are just working.
  const live = [...(running.data ?? []), ...(paused.data ?? [])];
  const waiting = live.filter((r) => (r.pending_approvals ?? 0) > 0);
  const active = live.filter((r) => !waiting.includes(r));
  const recent = useRunsList({ limit: 10 });
  return (
    <Page width="wide" className="max-w-[1240px]">
      <PageHeader eyebrow={today()} title="What should the council work on?"
        description="Describe the task. The council plans, works and checks itself, and you can step in at any point."
        actions={<TemplatePicker />} />
      <div className="grid gap-x-10 gap-y-8 lg:grid-cols-[minmax(0,1fr)_17rem] xl:grid-cols-[minmax(0,1fr)_19rem]">
        <div className="flex flex-col gap-10 min-w-0">
          <Composer />
          {waiting.length > 0 && (
            <section aria-labelledby="waiting-h">
              <SectionTitle id="waiting-h" count={waiting.length} description="These runs are waiting for your approval.">Needs you</SectionTitle>
              <Card as="div" className="border-accent/40"><RunRowList runs={waiting} /></Card>
            </section>
          )}
          {active.length > 0 && (
            <section aria-labelledby="active-h">
              <SectionTitle id="active-h" count={active.length}>Active</SectionTitle>
              <Card as="div"><RunRowList runs={active} /></Card>
            </section>
          )}
          <section aria-labelledby="recent-h">
            <SectionTitle id="recent-h" actions={<Link to="/runs" className="text-sm text-accent underline min-h-6 inline-flex items-center">All runs</Link>}>
              Recent
            </SectionTitle>
            {recent.isLoading ? (
              <div className="space-y-2"><Skeleton className="h-14" /><Skeleton className="h-14" /></div>
            ) : recent.error ? (
              <Card as="div"><ErrorState error={recent.error} onRetry={() => void recent.refetch()} /></Card>
            ) : recent.data?.length ? (
              <Card as="div"><RunRowList runs={recent.data} /></Card>
            ) : (
              <Card as="div">
                <EmptyState titleAs="h3" icon={History} title="No runs yet" body="Try one of these:"
                  action={
                    <div className="flex flex-wrap justify-center gap-2">
                      {EXAMPLES.map((ex) => (
                        <Button key={ex.topic} size="sm" onClick={() => window.dispatchEvent(new CustomEvent("argus:prefill", { detail: ex }))}>
                          {ex.topic}
                        </Button>
                      ))}
                    </div>
                  } />
              </Card>
            )}
          </section>
        </div>
        <aside className="flex flex-col gap-8 lg:sticky lg:top-16 lg:self-start" aria-label="At a glance">
          <WeekGlance />
          <ToolHealth />
          <ProviderGlance />
        </aside>
      </div>
    </Page>
  );
}
