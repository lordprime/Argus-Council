// Form primitives specific to the Councils area: unit-suffixed number field, tag input
// and a compact switch, on the same tokens as components/ui. The bounded Stepper lives in components/ui.

import { X } from "lucide-react";
import { useEffect, useId, useRef, useState, type KeyboardEvent, type ReactNode } from "react";

import { SwitchTrack } from "@/components/ui";
import { cn } from "@/lib/cn";

const control =
  "w-full rounded-md border border-border-strong bg-surface px-3 text-base text-fg placeholder:text-fg-subtle " +
  "disabled:opacity-60 aria-[invalid=true]:border-danger";

function Help({ id, hint, error }: { id: string; hint?: ReactNode; error?: string }) {
  if (error) return <p id={`${id}-err`} className="text-xs text-danger" role="alert">{error}</p>;
  if (hint) return <p id={`${id}-hint`} className="text-xs text-fg-subtle">{hint}</p>;
  return null;
}
const describedBy = (id: string, hint?: ReactNode, error?: string) => (error ? `${id}-err` : hint ? `${id}-hint` : undefined);

// ------------------------------------------------------------------ NumberField
export interface NumberFieldProps {
  label: string;
  value: number | null;
  onChange: (v: number | null) => void;
  min?: number;
  max?: number;
  step?: number;
  integer?: boolean;
  /** Empty input means `null` (for example "no limit"). */
  nullable?: boolean;
  unit?: string;
  prefix?: string;
  hint?: ReactNode;
  error?: string;
  placeholder?: string;
  className?: string;
  onValidityChange?: (valid: boolean) => void;
}

/**
 * Keeps the raw text the user typed so intermediate states ("0.", "") do not fight the
 * controlled value; only valid numbers within bounds are committed via `onChange`.
 */
export function NumberField({
  label, value, onChange, min, max, step, integer, nullable, unit, prefix, hint, error, placeholder, className, onValidityChange,
}: NumberFieldProps) {
  const id = useId();
  const [draft, setDraft] = useState(value == null ? "" : String(value));
  const [localError, setLocalError] = useState<string>();
  const committed = useRef(value);

  useEffect(() => {
    // External change (reset, preset, server data): reflect it unless it is what we just committed.
    if (value !== committed.current) {
      committed.current = value;
      setDraft(value == null ? "" : String(value));
      setLocalError(undefined);
    }
  }, [value]);

  const validate = (raw: string): { ok: true; v: number | null } | { ok: false; msg: string } => {
    const t = raw.trim();
    if (t === "") return nullable ? { ok: true, v: null } : { ok: false, msg: "Enter a value." };
    const n = Number(t);
    if (!Number.isFinite(n)) return { ok: false, msg: "Enter a number." };
    if (integer && !Number.isInteger(n)) return { ok: false, msg: "Enter a whole number." };
    if (min != null && n < min) return { ok: false, msg: `Must be at least ${min}.` };
    if (max != null && n > max) return { ok: false, msg: `Must be at most ${max.toLocaleString()}.` };
    return { ok: true, v: n };
  };

  const change = (raw: string) => {
    setDraft(raw);
    const r = validate(raw);
    setLocalError(r.ok ? undefined : r.msg);
    onValidityChange?.(r.ok);
    if (r.ok) {
      committed.current = r.v;
      onChange(r.v);
    }
  };

  const shownError = localError ?? error;
  return (
    <div className={cn("flex flex-col gap-1", className)}>
      <label htmlFor={id} className="text-sm font-medium text-fg">{label}</label>
      <div className="relative flex items-center">
        {prefix && <span aria-hidden className="pointer-events-none absolute left-3 text-sm text-fg-subtle">{prefix}</span>}
        <input
          id={id}
          type="text"
          inputMode={integer ? "numeric" : "decimal"}
          className={cn(control, "h-9 tabular", prefix && "pl-6", unit && "pr-16")}
          value={draft}
          placeholder={placeholder}
          aria-invalid={Boolean(shownError) || undefined}
          aria-describedby={describedBy(id, hint, shownError)}
          onChange={(e) => change(e.target.value)}
          onKeyDown={(e) => {
            if (e.key !== "ArrowUp" && e.key !== "ArrowDown") return;
            e.preventDefault();
            const cur = Number(draft) || 0;
            const s = step ?? 1;
            const next = e.key === "ArrowUp" ? cur + s : cur - s;
            const bounded = Math.min(max ?? Infinity, Math.max(min ?? -Infinity, Number(next.toFixed(6))));
            change(String(bounded));
          }}
        />
        {unit && <span aria-hidden className="pointer-events-none absolute right-3 text-xs text-fg-subtle">{unit}</span>}
      </div>
      <Help id={id} hint={hint} error={shownError} />
    </div>
  );
}

// ------------------------------------------------------------------ TagInput
export function TagInput({ label, value, onChange, placeholder, hint, className }: {
  label: string;
  value: string[];
  onChange: (v: string[]) => void;
  placeholder?: string;
  hint?: ReactNode;
  className?: string;
}) {
  const id = useId();
  const [text, setText] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);

  const add = (raw: string) => {
    const parts = raw.split(",").map((s) => s.trim()).filter(Boolean);
    const next = [...value];
    for (const p of parts) if (!next.includes(p)) next.push(p);
    if (next.length !== value.length) onChange(next);
    setText("");
  };

  const onKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter" || e.key === ",") {
      e.preventDefault();
      if (text.trim()) add(text);
    } else if (e.key === "Backspace" && !text && value.length) {
      onChange(value.slice(0, -1));
    }
  };

  return (
    <div className={cn("flex flex-col gap-1", className)}>
      <label htmlFor={id} className="text-sm font-medium text-fg">{label}</label>
      {/* Clicking the padding focuses the input; keyboard users reach the input directly. */}
      {/* eslint-disable-next-line jsx-a11y/click-events-have-key-events, jsx-a11y/no-static-element-interactions */}
      <div
        className="flex flex-wrap items-center gap-1.5 min-h-9 rounded-md border border-border-strong bg-surface px-1.5 py-1 focus-within:outline focus-within:outline-2 focus-within:outline-offset-2 focus-within:outline-[rgb(var(--focus-ring))]"
        onClick={() => inputRef.current?.focus()}
      >
        {value.length > 0 && (
          <ul className="flex flex-wrap gap-1.5" aria-label={`${label} entries`}>
            {value.map((t) => (
              <li key={t} className="inline-flex items-center gap-0.5 rounded-sm bg-surface-2 border border-border pl-1.5 h-6 font-mono text-xs">
                {t}
                <button type="button" aria-label={`Remove ${t}`} title={`Remove ${t}`}
                  className="inline-flex items-center justify-center size-6 rounded-sm text-fg-subtle hover:text-fg hover:bg-surface-3"
                  onClick={(e) => { e.stopPropagation(); onChange(value.filter((x) => x !== t)); }}>
                  <X className="size-3" aria-hidden />
                </button>
              </li>
            ))}
          </ul>
        )}
        <input
          ref={inputRef}
          id={id}
          className="flex-1 min-w-24 h-7 bg-transparent px-1.5 text-base text-fg placeholder:text-fg-subtle focus-visible:outline-none"
          value={text}
          placeholder={placeholder}
          aria-describedby={hint ? `${id}-hint` : undefined}
          onChange={(e) => (e.target.value.includes(",") ? add(e.target.value) : setText(e.target.value))}
          onKeyDown={onKeyDown}
          onBlur={() => text.trim() && add(text)}
        />
      </div>
      <Help id={id} hint={hint} />
    </div>
  );
}

// ------------------------------------------------------------------ Switch with an accessible name but compact visual
export function MiniSwitch({ checked, onChange, label, disabled }: { checked: boolean; onChange: (v: boolean) => void; label: string; disabled?: boolean }) {
  return (
    <button type="button" role="switch" aria-checked={checked} aria-label={label} title={label} disabled={disabled}
      onClick={() => onChange(!checked)}
      className="inline-flex items-center gap-2 h-8 rounded-md px-1.5 text-xs text-fg-muted hover:bg-surface-2 disabled:opacity-50">
      <SwitchTrack checked={checked} />
      <span aria-hidden className="w-6 text-left">{checked ? "On" : "Off"}</span>
    </button>
  );
}
