// Page-level patterns built from the primitives in ./index (consistency audit C-21, C-24, C-28, C-29).
// Re-exported from "@/components/ui", so import them from there.
//
// This module and ./index import each other. Only reference ./index bindings inside functions (render
// time), never at module top level, so the import cycle stays safe whichever module loads first.

import { Check, Search, X, type LucideIcon } from "lucide-react";
import { useEffect, useId, useState, type ButtonHTMLAttributes, type ReactNode } from "react";
import { Link, type LinkProps } from "react-router";

import { cn } from "@/lib/cn";

import { Button, buttonClass, touchTarget, type ButtonSize, type ButtonVariant } from "./index";

// ------------------------------------------------------------------ ButtonLink
/** A router link that looks like a Button (navigation must stay a link, not a button with onClick). */
export function ButtonLink({ variant = "secondary", size = "md", icon: Icon, className, children, ...rest }: LinkProps & {
  variant?: ButtonVariant;
  size?: ButtonSize;
  icon?: LucideIcon;
}) {
  return (
    <Link className={cn(buttonClass(variant, size), className)} {...rest}>
      {Icon && <Icon className="size-4" aria-hidden />}
      {children}
    </Link>
  );
}

// ------------------------------------------------------------------ FormActions
type CancelAction = { label?: string } & ({ to: string } | { onClick: () => void });

/**
 * The one form action bar (C-21): the primary action first, secondary actions next to it, Cancel as a
 * ghost button (a link when it navigates away), and optional meta text pushed to the right. `sticky`
 * pins the bar to the bottom of the viewport above the mobile tab bar, for long forms.
 */
export function FormActions({ children, cancel, meta, sticky, size = "md", className }: {
  /** Primary action first, then secondary actions. */
  children: ReactNode;
  cancel?: CancelAction;
  /** Right-aligned status or summary ("Unsaved changes", "5 seats · mock/mock-1"). */
  meta?: ReactNode;
  sticky?: boolean;
  /** Size of the Cancel control, to match the primary button. */
  size?: ButtonSize;
  className?: string;
}) {
  const label = cancel?.label ?? "Cancel";
  return (
    <div className={cn("flex flex-wrap items-center gap-2",
      sticky && "sticky bottom-[calc(3.5rem+1px)] md:bottom-0 z-sticky -mx-4 md:mx-0 px-4 md:px-0 py-3 bg-canvas/95 border-t border-border",
      className)}>
      {children}
      {cancel && ("to" in cancel
        ? <ButtonLink to={cancel.to} variant="ghost" size={size}>{label}</ButtonLink>
        : <Button type="button" variant="ghost" size={size} onClick={cancel.onClick}>{label}</Button>)}
      {meta != null && meta !== false && <div className="ml-auto text-xs text-fg-subtle">{meta}</div>}
    </div>
  );
}

// ------------------------------------------------------------------ FilterBar
/**
 * The one list-filter layout (C-28): unboxed, every control's label on the same top line, filters apply
 * as you change them (debounce text with `useDebouncedValue`), and a ghost "Clear" at the end of the row.
 */
export function FilterBar({ label, children, onClear, canClear = true, className }: {
  /** Accessible name of the search landmark, e.g. "Filter runs". */
  label: string;
  children: ReactNode;
  onClear?: () => void;
  canClear?: boolean;
  className?: string;
}) {
  return (
    <div role="search" aria-label={label} className={cn("flex flex-wrap items-start gap-3", className)}>
      {children}
      {onClear && (
        <Button variant="ghost" icon={X} onClick={onClear} disabled={!canClear} className="self-end ml-auto">Clear</Button>
      )}
    </div>
  );
}

/** A labelled search input with a leading icon, sized like every other field. */
export function SearchField({ label, value, onChange, placeholder, className }: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  className?: string;
}) {
  const id = useId();
  return (
    <div className={cn("flex flex-col gap-1 min-w-0", className)}>
      <label htmlFor={id} className="text-sm font-medium text-fg">{label}</label>
      <div className="relative">
        <Search className="size-4 text-fg-subtle absolute left-3 top-1/2 -translate-y-1/2 pointer-events-none" aria-hidden />
        <input id={id} type="search" value={value} placeholder={placeholder} onChange={(e) => onChange(e.target.value)}
          className={cn("w-full h-9 rounded-md border border-border-strong bg-surface pl-9 pr-3 text-base text-fg placeholder:text-fg-subtle", touchTarget)} />
      </div>
    </div>
  );
}

/** `value`, updated only after it has been stable for `ms` (live filters on typed text). */
export function useDebouncedValue<T>(value: T, ms = 300): T {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setSettled(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return settled;
}

// ------------------------------------------------------------------ Titles
type TitleTag = "h2" | "h3" | "h4" | "p" | "span";

/** The one card-title token (C-29): `text-base` semibold. Pick the heading level that fits the outline. */
export function CardTitle({ as: As = "h3", id, title, className, children }: {
  as?: TitleTag;
  id?: string;
  title?: string;
  className?: string;
  children: ReactNode;
}) {
  return <As id={id} title={title} className={cn("text-base font-semibold text-fg", className)}>{children}</As>;
}

/** A section heading (`text-lg`) with an optional muted count, description and actions on the right. */
export function SectionTitle({ id, as: As = "h2", children, count, description, actions, className }: {
  id?: string;
  as?: "h2" | "h3";
  children: ReactNode;
  count?: number;
  description?: ReactNode;
  actions?: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("flex flex-wrap items-end justify-between gap-2 mb-3", className)}>
      <div className="min-w-0">
        <As id={id} className="text-lg">
          {children}
          {count != null && <span className="text-fg-subtle font-normal tabular"> ({count})</span>}
        </As>
        {description && <p className="text-sm text-fg-muted">{description}</p>}
      </div>
      {actions}
    </div>
  );
}

// ------------------------------------------------------------------ Chip and Tag
/**
 * A toggle chip (C-24): rounded-full, `aria-pressed`, and a check mark when on so the state never
 * relies on colour alone. For multi-select option sets (deliverable formats, skill tools).
 */
export function Chip({ pressed, onPressedChange, mono, className, children, ...rest }:
  Omit<ButtonHTMLAttributes<HTMLButtonElement>, "onChange" | "onClick" | "aria-pressed"> & {
    pressed: boolean;
    onPressedChange: (pressed: boolean) => void;
    mono?: boolean;
  }) {
  return (
    <button type="button" aria-pressed={pressed} onClick={() => onPressedChange(!pressed)}
      className={cn("inline-flex items-center gap-1 h-8 px-3 rounded-full border transition-colors duration-fast whitespace-nowrap",
        "disabled:opacity-50 disabled:cursor-not-allowed", touchTarget,
        mono ? "font-mono text-xs" : "text-sm",
        pressed ? "bg-accent-subtle border-accent text-accent font-medium" : "border-border-strong text-fg-muted hover:text-fg hover:bg-surface-2",
        className)}
      {...rest}>
      {pressed && <Check className="size-3.5 -ml-1" aria-hidden />}
      {children}
    </button>
  );
}

/** A static tag (C-24): rounded-sm, monospace. For tool names, risk tags and model ids. */
export function Tag({ as: As = "span", title, className, children }: {
  as?: "span" | "li";
  title?: string;
  className?: string;
  children: ReactNode;
}) {
  return (
    <As title={title} className={cn("inline-flex items-center h-6 max-w-full rounded-sm border border-border bg-surface-2 px-1.5 font-mono text-xs text-fg-muted", className)}>
      <span className="truncate">{children}</span>
    </As>
  );
}

/** A labelled list of Tags, with an optional cap ("+3 more") and an empty text. */
export function TagList({ items, label, max, empty = "None", className }: {
  items: string[];
  label: string;
  max?: number;
  empty?: string;
  className?: string;
}) {
  if (!items.length) return <span className="text-sm text-fg-subtle">{empty}</span>;
  const shown = max != null ? items.slice(0, max) : items;
  const extra = items.length - shown.length;
  return (
    <ul aria-label={label} className={cn("flex flex-wrap gap-1", className)}>
      {shown.map((t) => <Tag key={t} as="li" title={t}>{t}</Tag>)}
      {extra > 0 && <li className="inline-flex items-center h-6 px-1 text-xs text-fg-subtle">+{extra} more</li>}
    </ul>
  );
}
