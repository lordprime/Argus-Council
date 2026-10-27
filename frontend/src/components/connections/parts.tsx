import { Plus, Trash2 } from "lucide-react";
import { useId, type ReactNode } from "react";

import { Button, IconButton, SwitchTrack, TagList, touchTarget } from "@/components/ui";
import { cn } from "@/lib/cn";

/** Small monospace tags (tools, risk tags, models): the shared TagList. */
export const Chips = TagList;

/** Section headings moved to components/ui; re-exported for existing imports. */
export { SectionTitle } from "@/components/ui";

/** A definition-list row used inside connection cards. */
export function Meta({ label, children, mono }: { label: string; children: ReactNode; mono?: boolean }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs text-fg-subtle">{label}</dt>
      <dd className={cn("text-sm text-fg break-words", mono && "font-mono text-xs leading-5")}>{children}</dd>
    </div>
  );
}

export interface KV {
  key: string;
  value: string;
}

/** Editable name/value rows whose values are secrets (password inputs, never prefilled). */
export function SecretRows({ legend, rows, onChange, namePlaceholder, hint }: {
  legend: string;
  rows: KV[];
  onChange: (rows: KV[]) => void;
  namePlaceholder: string;
  hint?: string;
}) {
  const id = useId();
  const set = (i: number, patch: Partial<KV>) => onChange(rows.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  return (
    <fieldset className="flex flex-col gap-2" aria-describedby={hint ? `${id}-hint` : undefined}>
      <legend className="text-sm font-medium mb-1">{legend}</legend>
      {rows.map((r, i) => (
        <div key={i} className="grid grid-cols-[1fr_1fr_auto] gap-2 items-center">
          <input aria-label={`${legend} ${i + 1} name`} value={r.key} placeholder={namePlaceholder} spellCheck={false}
            onChange={(e) => set(i, { key: e.target.value })}
            className="h-9 w-full min-w-0 rounded-md border border-border-strong bg-surface px-3 text-sm font-mono placeholder:text-fg-subtle" />
          <input aria-label={`${legend} ${i + 1} value`} type="password" autoComplete="new-password" value={r.value}
            placeholder="Value (write-only)" spellCheck={false} onChange={(e) => set(i, { value: e.target.value })}
            className="h-9 w-full min-w-0 rounded-md border border-border-strong bg-surface px-3 text-sm font-mono placeholder:text-fg-subtle" />
          <IconButton type="button" label={`Remove ${legend.toLowerCase()} ${r.key || i + 1}`} icon={Trash2}
            onClick={() => onChange(rows.filter((_, j) => j !== i))} />
        </div>
      ))}
      <div>
        <Button type="button" size="sm" variant="ghost" icon={Plus} onClick={() => onChange([...rows, { key: "", value: "" }])}>
          Add {legend.toLowerCase().replace(/s$/, "")}
        </Button>
      </div>
      {hint && <p id={`${id}-hint`} className="text-xs text-fg-subtle">{hint}</p>}
    </fieldset>
  );
}

export const rowsToDict = (rows: KV[]) =>
  Object.fromEntries(rows.filter((r) => r.key.trim() && r.value).map((r) => [r.key.trim(), r.value]));

export const splitList = (s: string) => [...new Set(s.split(/[,\n]/).map((x) => x.trim()).filter(Boolean))];

/** Grid of loading cards matching the final card layout. */
export function CardSkeletons({ count = 3, className }: { count?: number; className?: string }) {
  return (
    <div className={cn("grid gap-4 md:grid-cols-2", className)} role="status" aria-busy="true">
      <span className="sr-only">Loading…</span>
      {Array.from({ length: count }, (_, i) => (
        <div key={i} aria-hidden className="rounded-lg border border-border bg-surface p-4 space-y-3">
          <div className="flex justify-between"><div className="h-5 w-40 animate-pulse rounded-md bg-surface-3" /><div className="h-5 w-20 animate-pulse rounded-md bg-surface-3" /></div>
          <div className="h-4 w-3/4 animate-pulse rounded-md bg-surface-3" />
          <div className="h-4 w-1/2 animate-pulse rounded-md bg-surface-3" />
          <div className="h-8 w-full animate-pulse rounded-md bg-surface-3" />
        </div>
      ))}
    </div>
  );
}

/**
 * Compact switch for list cards. The visible text stays short ("Enabled") while the accessible name carries the
 * object, e.g. "Enabled (code-review)", so a page of cards does not read as a row of identical switches.
 */
export function Switch({ label = "Enabled", object, checked, onChange, disabled }: {
  label?: string;
  object: string;
  checked: boolean;
  onChange: (v: boolean) => void;
  disabled?: boolean;
}) {
  return (
    <button type="button" role="switch" aria-checked={checked} aria-label={`${label} (${object})`} disabled={disabled}
      onClick={() => onChange(!checked)}
      className={cn("inline-flex items-center gap-2 min-h-6 text-sm font-medium disabled:opacity-50 disabled:cursor-not-allowed", touchTarget)}>
      <SwitchTrack checked={checked} />
      {label}
    </button>
  );
}
