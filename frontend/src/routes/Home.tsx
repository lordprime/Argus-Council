import { Code2, FileText, MessageCircle, Sparkles, Wand2 } from "lucide-react";
import { useEffect, useMemo, useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router";

import { RunRow } from "@/components/run/RunRow";
import { Button, Callout, Card, EmptyState, ErrorState, Segmented, SelectField, Skeleton, TextArea, TextField, Toggle } from "@/components/ui";
import { RoleAvatar } from "@/components/ui/role";
import { SaveTemplateButton, TemplatePicker } from "@/features/templates/components";
import { errorMessage } from "@/lib/api";
import { useCouncils, useCreateRun, useProviderKinds, useProviders, useRoles, useRunsList } from "@/lib/queries";
import type { Mode, OutputFormat } from "@/lib/types";

const MODES: { value: Mode; label: string; hint: string; icon: typeof Sparkles }[] = [
  { value: "research", label: "Research", hint: "Gather, analyze, cite, write.", icon: FileText },
  { value: "coding", label: "Coding", hint: "Design, implement, test in a sandbox.", icon: Code2 },
  { value: "chat", label: "Chat", hint: "Conversational with oversight.", icon: MessageCircle },
  { value: "general", label: "General", hint: "Anything else.", icon: Wand2 },
];
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

function Composer() {
  const navigate = useNavigate();
  const roles = useRoles();
  const councils = useCouncils();
  const kinds = useProviderKinds();
  const providers = useProviders();
  const create = useCreateRun();
  const [topic, setTopic] = useState("");
  const [description, setDescription] = useState("");
  const [mode, setMode] = useState<Mode>("research");
  const [councilId, setCouncilId] = useState("");
  const [size, setSize] = useState(DEFAULT_SIZE.research);
  const [outputs, setOutputs] = useState<OutputFormat[]>(["md"]);
  const [preset, setPreset] = useState<keyof typeof PRESETS>("balanced");
  const [advanced, setAdvanced] = useState(false);
  const [budget, setBudget] = useState("5");
  const [maxSteps, setMaxSteps] = useState("80");
  const [interceptMode, setInterceptMode] = useState("hold");
  const [approvals, setApprovals] = useState<string[]>(["shell", "package_install", "remote_agent", "network_write"]);
  const [route, setRoute] = useState("mock::mock-1");

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
      const models = p.default_model ? [p.default_model] : kind?.suggested_models ?? [];
      for (const m of models) out.push({ value: `${p.name}::${m}`, label: `${p.name} · ${m}` });
    }
    for (const sys of kinds.data?.system_providers ?? []) {
      const kind = kinds.data?.kinds.find((k) => k.key === sys);
      for (const m of kind?.suggested_models.length ? kind.suggested_models : sys === "mock" ? ["mock-1"] : [])
        out.push({ value: `${sys}::${m}`, label: `${sys === "mock" ? "Offline demo (mock)" : sys} · ${m}` });
    }
    return out;
  }, [providers.data, kinds.data]);

  const onlyMock = routes.length > 0 && routes.every((r) => r.value.startsWith("mock::"));

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
      const run = await create.mutateAsync(buildConfig());
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
              hint={onlyMock ? "Only the offline demo model is available. Connect a provider for real runs." : undefined}>
              {routes.map((r) => <option key={r.value} value={r.value}>{r.label}</option>)}
            </SelectField>
          )}
        </div>

        {!councilId && (
          <div className="flex flex-col gap-2">
            <div className="flex items-center gap-3">
              <span className="text-sm font-medium" id="size-label">Council size</span>
              <div className="inline-flex items-center rounded-md border border-border-strong" role="group" aria-labelledby="size-label">
                <button type="button" aria-label="Fewer seats" className="h-8 w-8" onClick={() => setSize((s) => Math.max(1, s - 1))}>−</button>
                <span className="w-8 text-center tabular" aria-live="polite">{size}</span>
                <button type="button" aria-label="More seats" className="h-8 w-8" onClick={() => setSize((s) => Math.min(12, s + 1))}>+</button>
              </div>
            </div>
            <ul className="flex flex-wrap gap-1.5" aria-label="Roster preview">
              {roster.map((r, i) => (
                <li key={`${r}-${i}`} className="inline-flex items-center gap-1.5 rounded-full border border-border bg-surface-2 pl-1 pr-2.5 h-7 text-sm">
                  <RoleAvatar role={r} overseer={overseers.has(r)} size={20} />
                  {roles.data?.roles.find((x) => x.key === r)?.title ?? r}
                </li>
              ))}
            </ul>
          </div>
        )}

        <fieldset>
          <legend className="text-sm font-medium mb-2">Deliverable formats</legend>
          <div className="flex flex-wrap gap-2">
            {FORMATS.map((f) => {
              const on = outputs.includes(f.value);
              return (
                <button key={f.value} type="button" aria-pressed={on}
                  onClick={() => setOutputs((o) => (on ? o.filter((x) => x !== f.value) : [...o, f.value]))}
                  className={`h-8 px-3 rounded-full border text-sm ${on ? "bg-accent-subtle border-accent text-accent font-medium" : "border-border-strong text-fg-muted"}`}>
                  {f.label}
                </button>
              );
            })}
          </div>
        </fieldset>

        <div className="grid gap-4 sm:grid-cols-2">
          <SelectField label="Guardrails" value={preset} onChange={(e) => setPreset(e.target.value as keyof typeof PRESETS)}
            hint="Budget, step limits and how strongly overseers intervene.">
            <option value="strict">Strict</option>
            <option value="balanced">Balanced (default)</option>
            <option value="permissive">Permissive</option>
          </SelectField>
          <div className="flex items-end">
            <Button type="button" variant="ghost" onClick={() => setAdvanced((a) => !a)} aria-expanded={advanced}>
              {advanced ? "Hide advanced" : "Advanced guardrails"}
            </Button>
          </div>
        </div>
        {advanced && (
          <div className="grid gap-4 sm:grid-cols-3 rounded-lg bg-surface-2 p-4">
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
        {onlyMock && (
          <Callout tone="info" title="Running in offline demo mode"
            action={<Link to="/connections/providers" className="text-sm text-accent underline self-center">Connect a provider</Link>}>
            Councils will use a deterministic mock model until you connect Anthropic, OpenAI, Gemini, Ollama or another provider.
          </Callout>
        )}
        <div className="flex items-center gap-3">
          <Button type="submit" variant="primary" size="lg" icon={Sparkles} loading={create.isPending} disabled={!topic.trim()}>
            Start run
          </Button>
          <span className="text-xs text-fg-subtle">⌘↵</span>
          <span className="ml-auto"><SaveTemplateButton getConfig={buildConfig} disabled={!topic.trim()} /></span>
        </div>
      </form>
    </Card>
  );
}

export default function Home() {
  const active = useRunsList({ status: "running", limit: 10 });
  const recent = useRunsList({ limit: 10 });
  return (
    <div className="max-w-[880px] mx-auto p-4 md:p-6 flex flex-col gap-8">
      <div className="flex flex-wrap items-end gap-3">
        <div className="flex-1 min-w-0">
          <h1 className="text-2xl">New run</h1>
          <p className="text-sm text-fg-muted mt-1">Give the council a topic. It plans, works and checks itself, and you can step in at any point.</p>
        </div>
        <TemplatePicker />
      </div>
      <Composer />
      {active.data && active.data.length > 0 && (
        <section aria-labelledby="active-h">
          <h2 id="active-h" className="text-lg mb-2">Active</h2>
          <Card as="div"><ul className="divide-y divide-border">{active.data.map((r) => <RunRow key={r.id} run={r} />)}</ul></Card>
        </section>
      )}
      <section aria-labelledby="recent-h">
        <div className="flex items-baseline justify-between mb-2">
          <h2 id="recent-h" className="text-lg">Recent</h2>
          <Link to="/runs" className="text-sm text-accent underline">All runs</Link>
        </div>
        {recent.isLoading ? (
          <div className="space-y-2"><Skeleton className="h-14" /><Skeleton className="h-14" /></div>
        ) : recent.error ? (
          <ErrorState error={recent.error} onRetry={() => void recent.refetch()} />
        ) : recent.data?.length ? (
          <Card as="div"><ul className="divide-y divide-border">{recent.data.map((r) => <RunRow key={r.id} run={r} />)}</ul></Card>
        ) : (
          <Card as="div">
            <EmptyState icon={Sparkles} title="No runs yet" body="Try one of these:"
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
  );
}
