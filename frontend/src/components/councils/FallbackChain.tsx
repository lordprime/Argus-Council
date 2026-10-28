// Per-seat fallback chain: the routes a seat moves to, in order, when its provider fails.
// For CLI agents (account) seats, quota, rate-limit, sign-in and availability errors trigger it; the
// switch is recorded on the run as a provider.fallback event.

import { ArrowDown, ArrowUp, Plus, Trash2 } from "lucide-react";

import { AuthBadge } from "@/components/councils/ModelPicker";
import { Button, IconButton, SelectField, TextField } from "@/components/ui";
import { useProviderOptions } from "@/features/councils/queries";

export interface Route {
  provider: string;
  model: string;
}

export const MAX_FALLBACKS = 4;

export function FallbackChain({ value, onChange, primary }: {
  value: Route[];
  onChange: (next: Route[]) => void;
  primary: string;
}) {
  const { options } = useProviderOptions();
  const set = (i: number, patch: Partial<Route>) => onChange(value.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  const move = (i: number, d: -1 | 1) => {
    const a = value[i], b = value[i + d];
    if (!a || !b) return;
    const next = [...value];
    next[i] = b;
    next[i + d] = a;
    onChange(next);
  };
  const suggest = (): Route => {
    // CLI seat: suggest the same vendor's API provider; API seat: suggest a CLI agent, else any other option.
    const pick = options.find((o) => o.value !== primary && o.auth !== options.find((p) => p.value === primary)?.auth) ??
      options.find((o) => o.value !== primary) ?? options[0];
    return { provider: pick?.value ?? "mock", model: pick?.models[0] ?? "" };
  };

  return (
    <fieldset className="flex flex-col gap-2">
      <legend className="text-sm font-medium">Fallback chain</legend>
      <p className="text-xs text-fg-subtle">
        Tried in order when this seat's provider fails. A CLI agent falls back on usage limits, rate limits, sign-in or
        availability errors, e.g. Claude Code (account) then the Anthropic API key, or the other way round.
      </p>
      {value.length > 0 && (
        <ol className="flex flex-col gap-2" aria-label="Fallback routes">
          {value.map((r, i) => {
            const opt = options.find((o) => o.value === r.provider);
            return (
              <li key={i} className="grid gap-2 sm:grid-cols-[1fr_1fr_auto] items-end rounded-md border border-border p-2">
                <SelectField label={`Fallback ${i + 1} provider`} value={r.provider}
                  onChange={(e) => set(i, { provider: e.target.value, model: options.find((o) => o.value === e.target.value)?.models[0] ?? "" })}>
                  {!opt && <option value={r.provider}>{r.provider} (not configured)</option>}
                  {options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
                </SelectField>
                <TextField label={`Fallback ${i + 1} model`} value={r.model} spellCheck={false} autoComplete="off"
                  onChange={(e) => set(i, { model: e.target.value })} />
                <div className="flex items-center gap-0.5 pb-1">
                  <AuthBadge auth={opt?.auth} />
                  <IconButton label={`Move fallback ${i + 1} up`} icon={ArrowUp} disabled={i === 0} onClick={() => move(i, -1)} />
                  <IconButton label={`Move fallback ${i + 1} down`} icon={ArrowDown} disabled={i === value.length - 1} onClick={() => move(i, 1)} />
                  <IconButton label={`Remove fallback ${i + 1}`} icon={Trash2} onClick={() => onChange(value.filter((_, j) => j !== i))} />
                </div>
              </li>
            );
          })}
        </ol>
      )}
      <Button type="button" size="sm" icon={Plus} className="self-start" disabled={value.length >= MAX_FALLBACKS}
        onClick={() => onChange([...value, suggest()])}>
        Add fallback
      </Button>
    </fieldset>
  );
}
