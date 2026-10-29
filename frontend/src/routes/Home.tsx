import { BookPlus, ChevronDown, ChevronRight, History, Play } from "lucide-react";
import { useEffect, useMemo, useState, type FormEvent } from "react";
import { Link, useLocation, useNavigate } from "react-router";

import { RoleChip } from "@/components/councils/parts";
import { KnowledgePicker, QuickAddKnowledgeDialog } from "@/components/knowledge/AddKnowledge";
import { RunRowList } from "@/components/run/RunRow";
import {
  Button, Callout, Card, Chip, EmptyState, ErrorState, FormActions, Kbd, Page, PageHeader, SectionTitle, Segmented, SelectField, Skeleton,
  Stepper, TextArea, TextField, Toggle,
} from "@/components/ui";
import { MODES } from "@/features/councils/logic";
import { useKnowledgeBases } from "@/features/knowledge/queries";
import { SaveTemplateButton, TemplatePicker } from "@/features/templates/components";
import { errorMessage } from "@/lib/api";
import { providerKindLabel } from "@/lib/providers";
import { useCouncils, useCreateRun, useProviderKinds, useProviders, useRoles, useRunsList } from "@/lib/queries";
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
  const [councilId, setCouncilId] = useState("");
  const [size, setSize] = useState(DEFAULT_SIZE.research);
  const [outputs, setOutputs] = useState<OutputFormat[]>(["md"]);
  const [preset, setPreset] = useState<keyof typeof PRESETS>("balanced");
  const [advanced, setAdvanced] = useState(false);
  const [budget, setBudget] = useState("5");
  const [maxSteps, setMaxSteps] = useState("80");
  const [interceptMode, setInterceptMode] = useState("hold");
  const [approvals, setApprovals] = useState<string[]>(["shell", "package_install", "remote_agent", "network_write"]);
  const [route, setRoute] = useState("");
  const knowledge = useKnowledgeBases();
  const [kbIds, setKbIds] = useState<string[]>([]);
  const [addingKnowledge, setAddingKnowledge] = useState(false);

  useEffect(() => setSize(DEFAULT_SIZE[mode]), [mode]);
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
  // Keep the selection on a route that exists: the first one until the user picks.
  useEffect(() => {
    if (routes.length && !routes.some((r) => r.value === route)) setRoute(routes[0]!.value);
  }, [routes, route]);

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
    return {
      topic: topic.trim(), description, mode, outputs,
      ...(councilId ? { council_id: councilId } : { size, provider, model }),
      guardrails,
    };
  };

  const submit = async (e?: FormEvent) => {
    e?.preventDefault();
    if (!topic.trim()) return;
    try {
      const run = await create.mutateAsync({ ...buildConfig(), ...(kbIds.length ? { knowledge_base_ids: kbIds } : {}) });
      navigate(`/runs/${run.id}`);
    } catch {
      /* rendered below */
    }
  };

  return (
    <Card className="p-5">
      {/* ⌘↵ submits from any field in the composer. */}
      {/* eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions */}
      <form onSubmit={submit} className="flex flex-col gap-5"
        onKeyDown={(e) => { if ((e.metaKey || e.ctrlKey) && e.key === "Enter") void submit(); }}>
        <TextField label="Topic" required maxLength={200} autoFocus placeholder="What should the council work on?"
          value={topic} onChange={(e) => setTopic(e.target.value)} />
        <TextArea label="Description" maxLength={4000} placeholder="Details, constraints, what a great result looks like (Markdown)"
          hint={`${description.length}/4000`} value={description} onChange={(e) => setDescription(e.target.value)} />
        <Segmented label="Mode" value={mode} onChange={setMode} options={MODES} />
        <p className="text-xs text-fg-subtle -mt-3">{MODES.find((m) => m.value === mode)?.hint}</p>

        <div className="grid gap-4 sm:grid-cols-2">
          <SelectField label="Council" value={councilId} onChange={(e) => setCouncilId(e.target.value)}>
            <option value="">Auto from mode</option>
            {councils.data?.map((c) => <option key={c.id} value={c.id}>{c.name} ({c.members.length} seats)</option>)}
          </SelectField>
          {!councilId && (
            <SelectField label="Model" value={route} onChange={(e) => setRoute(e.target.value)}
              hint={noProviders ? "No provider connected yet." : undefined}>
              {routes.length ? routes.map((r) => <option key={r.value} value={r.value}>{r.label}</option>)
                : <option value="">No models available</option>}
            </SelectField>
          )}
        </div>

        {!councilId && (
          <div className="flex flex-col gap-2">
            <Stepper label="Council size" value={size} min={MIN_SEATS} max={MAX_SEATS} onChange={setSize}
              hint={`Between ${MIN_SEATS} and ${MAX_SEATS} seats.`} />
            <ul className="flex flex-wrap gap-1.5" aria-label="Roster preview">
              {roster.map((r, i) => <RoleChip key={`${r}-${i}`} role={r} roles={roles.data?.roles} overseer={overseers.has(r)} />)}
            </ul>
          </div>
        )}

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

        <div className="flex flex-col gap-2">
          <SelectField label="Guardrails" value={preset} onChange={(e) => setPreset(e.target.value as keyof typeof PRESETS)}
            hint="Budget, step limits and how strongly overseers intervene." className="sm:max-w-[calc(50%-0.5rem)]">
            <option value="strict">Strict</option>
            <option value="balanced">Balanced (default)</option>
            <option value="permissive">Permissive</option>
          </SelectField>
          <Button type="button" variant="ghost" size="sm" className="self-start -ml-2.5" icon={advanced ? ChevronDown : ChevronRight}
            onClick={() => setAdvanced((a) => !a)} aria-expanded={advanced} aria-controls="advanced-guardrails">
            {advanced ? "Hide advanced guardrails" : "Advanced guardrails"}
          </Button>
        </div>
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
              <div className="grid gap-3 sm:grid-cols-2">
                {APPROVALS.map((a) => (
                  <Toggle key={a.key} label={a.label} checked={approvals.includes(a.key)}
                    onChange={(on) => setApprovals((x) => (on ? [...x, a.key] : x.filter((k) => k !== a.key)))} />
                ))}
              </div>
            </fieldset>
          </div>
        )}

        {create.error && <Callout tone="danger" title="Could not start the run">{errorMessage(create.error)}</Callout>}
        {noProviders && (
          <Callout tone="warning" title="No model provider connected"
            action={<Link to="/connections/providers" className="text-sm text-accent underline self-center">Connect a provider</Link>}>
            Connect Anthropic, OpenAI, Gemini, Ollama or another provider (or a CLI agent) before starting a run.
          </Callout>
        )}
        <FormActions
          meta={topic.trim()
            ? <span className="[@media(pointer:coarse)]:hidden"><Kbd>⌘</Kbd> <Kbd>↵</Kbd> to start</span>
            : <span id="start-hint">Add a topic to start.</span>}>
          <Button type="submit" variant="primary" size="lg" icon={Play} loading={create.isPending} disabled={!topic.trim() || (!councilId && !route)}
            aria-describedby={!topic.trim() ? "start-hint" : undefined}>
            Start run
          </Button>
          <SaveTemplateButton getConfig={buildConfig} disabled={!topic.trim()} />
        </FormActions>
      </form>
    </Card>
  );
}

export default function Home() {
  const active = useRunsList({ status: "running", limit: 10 });
  const recent = useRunsList({ limit: 10 });
  return (
    <Page width="form">
      <PageHeader title="New run"
        description="Give the council a topic. It plans, works and checks itself, and you can step in at any point."
        actions={<TemplatePicker />} />
      <div className="flex flex-col gap-8">
        <Composer />
        {active.data && active.data.length > 0 && (
          <section aria-labelledby="active-h">
            <SectionTitle id="active-h" count={active.data.length}>Active</SectionTitle>
            <Card as="div"><RunRowList runs={active.data} /></Card>
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
    </Page>
  );
}
