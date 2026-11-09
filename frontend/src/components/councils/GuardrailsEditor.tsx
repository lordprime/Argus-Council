// Guardrails form (ui-spec §3.9). Edits a *partial* guardrails dict: fields show the backend
// default until changed, and setting a field back to its default drops the override.

import { Eye, Hand, ShieldX } from "lucide-react";
import { useCallback, useEffect, useState, type ReactNode } from "react";

import { NumberField, TagInput } from "@/components/councils/fields";
import { Badge, Button, Segmented, Toggle } from "@/components/ui";
import { countOverrides, effective, setOverride, type Dict } from "@/features/councils/logic";

export const APPROVAL_KEYS: { key: string; label: string; hint: string }[] = [
  { key: "shell", label: "Shell commands", hint: "Any command run in the sandbox." },
  { key: "network_write", label: "Network writes", hint: "POST/PUT requests and uploads." },
  { key: "remote_agent", label: "Remote agents", hint: "Delegating work over A2A." },
  { key: "package_install", label: "Installing packages", hint: "pip, npm and similar." },
  { key: "fs_write", label: "File writes", hint: "Creating or changing files in the workspace." },
];

const INTERCEPT: Record<string, { label: string; text: string; icon: typeof Eye }> = {
  annotate: { label: "Annotate", text: "Overseers comment only; the agent keeps going.", icon: Eye },
  hold: { label: "Hold", text: "The agent pauses until the correction lands.", icon: Hand },
  block: { label: "Block", text: "The flagged step is discarded.", icon: ShieldX },
};

const SENSITIVITY = [
  { value: "off", label: "Off" },
  { value: "low", label: "Low" },
  { value: "medium", label: "Medium" },
  { value: "high", label: "High" },
] as const;

const CONTENT = [
  { value: "off", label: "Off", hint: "No content screening." },
  { value: "standard", label: "Standard", hint: "Flag clearly harmful requests." },
  { value: "strict", label: "Strict", hint: "Also flag borderline topics." },
] as const;

const GLOB_HELP = (
  <>Glob patterns: <code className="font-mono">*</code> matches anything, for example <code className="font-mono">fs_*</code> or{" "}
    <code className="font-mono">shell:rm -rf*</code>. Press Enter or comma to add.</>
);

function Group({ legend, children, aside }: { legend: string; children: ReactNode; aside?: ReactNode }) {
  return (
    <fieldset className="min-w-0 rounded-lg border border-border p-4 flex flex-col gap-4">
      <legend className="px-1 text-sm font-semibold">{legend}</legend>
      {aside}
      {children}
    </fieldset>
  );
}

export function GuardrailsEditor({ value, onChange, onValidChange }: {
  value: Dict;
  onChange: (next: Dict) => void;
  onValidChange?: (valid: boolean) => void;
}) {
  const [invalid, setInvalid] = useState<Set<string>>(new Set());
  const valid = invalid.size === 0;
  useEffect(() => onValidChange?.(valid), [valid, onValidChange]);
  const validity = useCallback((key: string) => (ok: boolean) =>
    setInvalid((s) => {
      if (ok === !s.has(key)) return s;
      const n = new Set(s);
      if (ok) n.delete(key);
      else n.add(key);
      return n;
    }), []);

  const get = <T,>(path: string[]) => effective(value, path) as T;
  const set = (path: string[], v: unknown) => onChange(setOverride(value, path, v));

  const approvals = get<string[]>(["approval_required"]);
  const autoMode = get<boolean>(["auto_mode"]);
  const intercept = get<string>(["oversight", "intercept_mode"]);
  const preset = get<string>(["content_policy", "preset"]);
  const custom = countOverrides(value);

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-sm text-fg-muted">
          {custom === 0 ? "Using the default limits." : `${custom} setting${custom === 1 ? "" : "s"} changed from the defaults.`}
          {" "}Runs can tighten these further.
        </p>
        {custom > 0 && (
          <Button type="button" size="sm" variant="ghost" onClick={() => onChange({})}>Reset to defaults</Button>
        )}
      </div>

      <Group legend="Limits">
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          <NumberField label="Budget" prefix="$" unit="USD" min={0} step={0.5} nullable placeholder="No limit"
            value={get<number | null>(["budget", "usd"])} onChange={(v) => set(["budget", "usd"], v)}
            hint="Empty means no dollar cap." onValidityChange={validity("usd")} />
          <NumberField label="Budget buffer" prefix="$" unit="USD" min={0} step={0.5}
            value={get<number>(["budget", "buffer_usd"])} onChange={(v) => set(["budget", "buffer_usd"], v ?? 0)}
            hint="Extra spend the run draws on, without asking, instead of halting at its budget." onValidityChange={validity("buffer")} />
          <NumberField label="Token cap" unit="tokens" min={0} step={100000} integer nullable placeholder="No limit"
            value={get<number | null>(["budget", "tokens"])} onChange={(v) => set(["budget", "tokens"], v)}
            hint="Input plus output tokens." onValidityChange={validity("tokens")} />
          <NumberField label="Max steps" unit="steps" min={1} max={10000} integer
            value={get<number>(["max_steps"])} onChange={(v) => set(["max_steps"], v)} onValidityChange={validity("steps")} />
          <NumberField label="Max runtime" unit="min" min={1} max={1440} step={5}
            value={get<number>(["max_runtime_minutes"])} onChange={(v) => set(["max_runtime_minutes"], v)}
            onValidityChange={validity("runtime")} />
          <NumberField label="Parallel agents" unit="agents" min={1} max={64} integer
            value={get<number>(["max_parallel_agents"])} onChange={(v) => set(["max_parallel_agents"], v)}
            hint="Seats working at the same time." onValidityChange={validity("parallel")} />
        </div>
        <Toggle label="Approve limit raises automatically" checked={get<boolean>(["extensions", "auto_approve"])}
          onChange={(on) => set(["extensions", "auto_approve"], on)}
          hint="When the run hits its step, turn, runtime or token limit, raise it without asking (a few times at most). The dollar budget is never raised." />
      </Group>

      <Group legend="Debate">
        <div className="grid gap-3 sm:grid-cols-2">
          <Toggle label="Debate" checked={get<boolean>(["debate", "enabled"])} onChange={(on) => set(["debate", "enabled"], on)}
            hint="Members make claims, challenge each other and co-sign the conclusion. Off: one synthesis and a majority vote." />
          <Toggle label="Devil's advocate" checked={get<boolean>(["debate", "devils_advocate"])} onChange={(on) => set(["debate", "devils_advocate"], on)}
            hint="One debater, picked at random, argues the other side. Only you are told who; the council is not." />
          <Toggle label="Light moves on the cheapest strong model" checked={get<string>(["debate", "light_model"]) !== "seat"}
            onChange={(on) => set(["debate", "light_model"], on ? "latest_cheapest" : "seat")}
            hint="Challenges, positions and signatures run as one call on the newest cheap model of each seat's provider. Answers and the conclusion keep the seat's model." />
        </div>
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          <NumberField label="Deliberation rounds" unit="rounds" min={0} max={6} integer
            value={get<number>(["debate", "rounds"])} onChange={(v) => set(["debate", "rounds"], v)}
            hint="Challenge-and-answer rounds after the work." onValidityChange={validity("debate.rounds")} />
          <NumberField label="Converge rounds" unit="rounds" min={0} max={6} integer
            value={get<number>(["debate", "converge_rounds"])} onChange={(v) => set(["debate", "converge_rounds"], v)}
            hint="Rounds of revising positions while claims are contested." onValidityChange={validity("debate.converge")} />
          <NumberField label="Consensus share" min={0.51} max={1} step={0.01}
            value={get<number>(["debate", "consensus"])} onChange={(v) => set(["debate", "consensus"], v)}
            hint="Share of debaters that must endorse a claim for it to be agreed: more than half, up to 1." onValidityChange={validity("debate.consensus")} />
          <NumberField label="Max challenges" unit="per run" min={0} max={500} integer
            value={get<number>(["debate", "max_challenges"])} onChange={(v) => set(["debate", "max_challenges"], v)}
            onValidityChange={validity("debate.challenges")} />
          <NumberField label="Max nudges" unit="times" min={0} max={6} integer
            value={get<number>(["debate", "max_nudges"])} onChange={(v) => set(["debate", "max_nudges"], v)}
            hint="Times a member who tries to finish is sent back to answer open challenges." onValidityChange={validity("debate.nudges")} />
          <NumberField label="Light model floor" unit="reasoning" min={0} max={100}
            value={get<number>(["debate", "light_min_reasoning"])} onChange={(v) => set(["debate", "light_min_reasoning"], v)}
            hint="The least reasoning score (0-100) a cheaper model needs to take a seat's light moves." onValidityChange={validity("debate.floor")} />
        </div>
      </Group>

      <Group legend="Helpers">
        <Toggle label="Members can spawn helpers" checked={get<boolean>(["spawn", "enabled"])} onChange={(on) => set(["spawn", "enabled"], on)}
          hint="Light sub-agents that join the council while it works and share the run's budget." />
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          <NumberField label="Max helpers" unit="per run" min={0} max={64} integer
            value={get<number>(["spawn", "max_agents"])} onChange={(v) => set(["spawn", "max_agents"], v)}
            onValidityChange={validity("spawn.agents")} />
          <NumberField label="Max depth" unit="levels" min={0} max={4} integer
            value={get<number>(["spawn", "max_depth"])} onChange={(v) => set(["spawn", "max_depth"], v)}
            hint="Helpers spawning helpers." onValidityChange={validity("spawn.depth")} />
          <NumberField label="Max live helpers" unit="at once" min={1} max={64} integer
            value={get<number>(["spawn", "max_live"])} onChange={(v) => set(["spawn", "max_live"], v)}
            onValidityChange={validity("spawn.live")} />
        </div>
      </Group>

      <Group legend="Context">
        <div className="grid gap-3 sm:grid-cols-2">
          <Toggle label="Knowledge graph context" checked={get<boolean>(["graph", "enabled"])} onChange={(on) => set(["graph", "enabled"], on)}
            hint="Send members short digests from the council's knowledge graph instead of raw upstream text." />
          <Toggle label="Prompt caching" checked={get<boolean>(["graph", "prompt_cache"])} onChange={(on) => set(["graph", "prompt_cache"], on)}
            hint="Cache the stable part of each prompt where the provider supports it." />
          <Toggle label="Inherit the board" checked={get<boolean>(["graph", "inherit_board"])} onChange={(on) => set(["graph", "inherit_board"], on)}
            hint="Start with the board of the latest earlier run of this council (its newest notes, marked as earlier)." />
          <Toggle label="Compress on the cheapest strong model" checked={get<string>(["context", "compressor"]) !== "seat"}
            onChange={(on) => set(["context", "compressor"], on ? "latest_cheapest" : "seat")}
            hint="Older turns are summarised on the newest cheap model of the seat's provider instead of the seat's own." />
        </div>
        <div className="grid gap-4 sm:grid-cols-2">
          <NumberField label="Digest size" unit="chars" min={200} max={20000} step={100} integer
            value={get<number>(["graph", "digest_chars"])} onChange={(v) => set(["graph", "digest_chars"], v)}
            hint="Per dependency." onValidityChange={validity("graph.digest")} />
          <NumberField label="Council recall" unit="nodes" min={0} max={100} integer
            value={get<number>(["graph", "council_recall"])} onChange={(v) => set(["graph", "council_recall"], v)}
            hint="What the council learned in earlier runs, shown at the start." onValidityChange={validity("graph.recall")} />
          <NumberField label="Compress harder from" unit="of the token ceiling" min={0.1} max={1} step={0.05}
            value={get<number>(["context", "pressure_start"])} onChange={(v) => set(["context", "pressure_start"], v)}
            hint="Once the run has used this share of its tokens (limit, raises left and reserve), every agent's history shrinks, to 40% at the ceiling. 1 turns it off."
            onValidityChange={validity("context.pressure")} />
          <NumberField label="Compressor floor" unit="reasoning" min={0} max={100}
            value={get<number>(["context", "compressor_min_reasoning"])} onChange={(v) => set(["context", "compressor_min_reasoning"], v)}
            hint="The least reasoning score (0-100) a cheaper model needs to summarise a seat's history." onValidityChange={validity("context.floor")} />
        </div>
      </Group>

      <Group legend="Tools">
        <div className="grid gap-4 md:grid-cols-2">
          <TagInput label="Allow" value={get<string[]>(["tools", "allow"])} onChange={(v) => set(["tools", "allow"], v)}
            placeholder="web_search, fs_*" hint={GLOB_HELP} />
          <TagInput label="Deny" value={get<string[]>(["tools", "deny"])} onChange={(v) => set(["tools", "deny"], v)}
            placeholder="shell:rm -rf*" hint="Deny wins over allow." />
        </div>
      </Group>

      <Group legend="Require my approval for">
        <Toggle label="Auto mode" checked={autoMode} onChange={(on) => set(["auto_mode"], on)}
          hint="Agents run without asking for approval. Blocked actions stay blocked." />
        <div className="grid gap-3 sm:grid-cols-2">
          {APPROVAL_KEYS.map((a) => (
            <Toggle key={a.key} label={a.label} hint={a.hint} checked={approvals.includes(a.key)}
              onChange={(on) => set(["approval_required"], on ? [...approvals, a.key] : approvals.filter((k) => k !== a.key))} />
          ))}
        </div>
      </Group>

      <Group legend="Oversight">
        <div className="grid gap-4 md:grid-cols-2">
          <div className="flex flex-col gap-2">
            <Segmented label="When an overseer intercepts" value={intercept as "hold"}
              onChange={(v) => set(["oversight", "intercept_mode"], v)}
              options={Object.entries(INTERCEPT).map(([k, o]) => ({ value: k as "hold", label: o.label, icon: o.icon, hint: o.text }))} />
            <ul className="text-xs text-fg-muted space-y-1" aria-label="Intercept modes">
              {Object.entries(INTERCEPT).map(([k, o]) => (
                <li key={k} className={k === intercept ? "text-fg" : undefined}>
                  <span className="font-semibold">{o.label}{k === intercept && " (selected)"}:</span> {o.text}
                </li>
              ))}
            </ul>
          </div>
          <div className="flex flex-col gap-2">
            <Segmented label="Monitor sensitivity" value={get<string>(["oversight", "monitor_sensitivity"]) as "medium"}
              onChange={(v) => set(["oversight", "monitor_sensitivity"], v)}
              options={SENSITIVITY.map((s) => ({ value: s.value as "medium", label: s.label }))} />
            <p className="text-xs text-fg-muted">How readily the monitor flags drift, loops and off-topic work.</p>
          </div>
        </div>
      </Group>

      <Group legend="Content policy">
        <div className="grid gap-4 md:grid-cols-2">
          <div className="flex flex-col gap-2">
            <Segmented label="Preset" value={preset as "standard"} onChange={(v) => set(["content_policy", "preset"], v)}
              options={CONTENT.map((c) => ({ value: c.value as "standard", label: c.label, hint: c.hint }))} />
            <p className="text-xs text-fg-muted">{CONTENT.find((c) => c.value === preset)?.hint}</p>
          </div>
          <TagInput label="Blocked topics" value={get<string[]>(["content_policy", "blocked_topics"])}
            onChange={(v) => set(["content_policy", "blocked_topics"], v)} placeholder="Add a topic"
            hint="Agents refuse work on these topics." />
        </div>
      </Group>
    </div>
  );
}

/** One-line summary of effective guardrails, for collapsed views. */
export function GuardrailsSummary({ value }: { value: Dict }) {
  const usd = effective(value, ["budget", "usd"]) as number | null;
  const steps = effective(value, ["max_steps"]) as number;
  const intercept = effective(value, ["oversight", "intercept_mode"]) as string;
  const approvals = (effective(value, ["approval_required"]) as string[]).length;
  const auto = effective(value, ["auto_mode"]) as boolean;
  return (
    <div className="flex flex-wrap gap-1.5">
      {auto && <Badge tone="accent">Auto mode</Badge>}
      <Badge>{usd == null ? "No $ cap" : `$${usd} budget`}</Badge>
      <Badge>{steps} steps</Badge>
      <Badge>{intercept} intercepts</Badge>
      {!auto && <Badge>{approvals} approvals</Badge>}
    </div>
  );
}
