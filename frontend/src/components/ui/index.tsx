// Design-system primitives (ui-spec §7.6). Small, accessible, token-driven.

import * as RDialog from "@radix-ui/react-dialog";
import * as RTabs from "@radix-ui/react-tabs";
import {
  AlertTriangle,
  ArrowLeft,
  CheckCircle2,
  CircleDashed,
  Eye,
  EyeOff,
  Info,
  Loader2,
  Minus,
  PauseCircle,
  Plus,
  ShieldX,
  X,
  XCircle,
  type LucideIcon,
} from "lucide-react";
import {
  forwardRef,
  useId,
  useRef,
  useState,
  type ButtonHTMLAttributes,
  type ElementType,
  type InputHTMLAttributes,
  type KeyboardEvent,
  type ReactNode,
  type SelectHTMLAttributes,
  type TextareaHTMLAttributes,
} from "react";
import { Link, NavLink } from "react-router";

import { ApiError } from "@/lib/api";
import { cn } from "@/lib/cn";
import { useToasts } from "@/stores/toast";

// ------------------------------------------------------------------ Button
type Variant = "primary" | "secondary" | "ghost" | "danger" | "danger-ghost";
type Size = "sm" | "md" | "lg";

const variants: Record<Variant, string> = {
  primary: "bg-accent text-accent-fg hover:bg-accent-hover shadow-1",
  secondary: "bg-surface text-fg border border-border-strong hover:bg-surface-2",
  ghost: "text-fg-muted hover:bg-surface-2 hover:text-fg",
  danger: "bg-danger text-danger-fg hover:opacity-90",
  "danger-ghost": "text-danger hover:bg-danger-subtle",
};
const sizes: Record<Size, string> = { sm: "h-8 px-2.5 text-sm gap-1.5", md: "h-9 px-3 text-base gap-2", lg: "h-10 px-4 text-base gap-2" };

/**
 * Touch-size variants (spec §7.6): on a coarse pointer every control grows to a 44px target. Desktop
 * sizes are untouched. `touchTarget` is for anything with a height, `touchSquare` for icon-only controls.
 */
export const touchTarget = "[@media(pointer:coarse)]:min-h-11";
export const touchSquare = "[@media(pointer:coarse)]:min-h-11 [@media(pointer:coarse)]:min-w-11";

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  size?: Size;
  loading?: boolean;
  icon?: LucideIcon;
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = "secondary", size = "md", loading, icon: Icon, className, children, disabled, ...rest },
  ref,
) {
  return (
    <button
      ref={ref}
      className={cn(
        "inline-flex items-center justify-center rounded-md font-medium transition-colors duration-fast",
        "disabled:opacity-50 disabled:cursor-not-allowed min-w-6 min-h-6 whitespace-nowrap",
        touchTarget,
        variants[variant],
        sizes[size],
        className,
      )}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      {...rest}
    >
      {loading ? <Loader2 className="size-4 animate-spin" aria-hidden /> : Icon ? <Icon className="size-4" aria-hidden /> : null}
      {children}
    </button>
  );
});

export const IconButton = forwardRef<HTMLButtonElement, ButtonProps & { label: string }>(function IconButton(
  { label, icon, size = "sm", variant = "ghost", className, ...rest },
  ref,
) {
  return (
    <Button ref={ref} aria-label={label} title={label} icon={icon} size={size} variant={variant}
      className={cn("px-0 aspect-square", touchSquare, className)} {...rest} />
  );
});

// ------------------------------------------------------------------ Form fields
interface FieldShell {
  label: string;
  hint?: ReactNode;
  error?: string;
  className?: string;
}

function Shell({ id, label, hint, error, className, children }: FieldShell & { id: string; children: ReactNode }) {
  return (
    <div className={cn("flex flex-col gap-1", className)}>
      <label htmlFor={id} className="text-sm font-medium text-fg">{label}</label>
      {children}
      {hint && !error && <p id={`${id}-hint`} className="text-xs text-fg-subtle">{hint}</p>}
      {error && <p id={`${id}-err`} className="text-xs text-danger" role="alert">{error}</p>}
    </div>
  );
}

/** Shared look of text inputs and selects (exported so area-specific fields stay identical). */
export const control =
  "w-full rounded-md border border-border-strong bg-surface px-3 text-base text-fg placeholder:text-fg-subtle " +
  "disabled:opacity-60 aria-[invalid=true]:border-danger " + touchTarget;

export function TextField({ label, hint, error, className, ...rest }: FieldShell & InputHTMLAttributes<HTMLInputElement>) {
  const id = useId();
  return (
    <Shell id={id} label={label} hint={hint} error={error} className={className}>
      <input id={id} className={cn(control, "h-9")} aria-invalid={Boolean(error) || undefined}
        aria-describedby={error ? `${id}-err` : hint ? `${id}-hint` : undefined} {...rest} />
    </Shell>
  );
}

/**
 * Password input with a show/hide toggle. The toggle's name is its visible text plus sr-only context
 * ("Show password"), never an aria-label, so the field stays the only element labelled "Password".
 */
export function PasswordField({ label, hint, error, className, ...rest }: FieldShell & Omit<InputHTMLAttributes<HTMLInputElement>, "type">) {
  const id = useId();
  const [visible, setVisible] = useState(false);
  return (
    <Shell id={id} label={label} hint={hint} error={error} className={className}>
      <div className="relative">
        <input id={id} type={visible ? "text" : "password"} className={cn(control, "h-9 pr-20")} aria-invalid={Boolean(error) || undefined}
          aria-describedby={error ? `${id}-err` : hint ? `${id}-hint` : undefined} spellCheck={false} autoCapitalize="off" {...rest} />
        <button type="button" aria-controls={id} onClick={() => setVisible((v) => !v)}
          className={cn("absolute right-1 top-1/2 -translate-y-1/2 inline-flex items-center gap-1 h-7 px-2 rounded-sm text-xs font-medium",
            "text-fg-muted hover:bg-surface-2 hover:text-fg", "[@media(pointer:coarse)]:h-10")}>
          {visible ? <EyeOff className="size-4" aria-hidden /> : <Eye className="size-4" aria-hidden />}
          {visible ? "Hide" : "Show"}<span className="sr-only"> {label.toLowerCase()}</span>
        </button>
      </div>
      <span className="sr-only" aria-live="polite">{visible ? `${label} is visible` : ""}</span>
    </Shell>
  );
}

export function TextArea({ label, hint, error, className, ...rest }: FieldShell & TextareaHTMLAttributes<HTMLTextAreaElement>) {
  const id = useId();
  return (
    <Shell id={id} label={label} hint={hint} error={error} className={className}>
      <textarea id={id} className={cn(control, "py-2 min-h-24 resize-y")} aria-invalid={Boolean(error) || undefined}
        aria-describedby={error ? `${id}-err` : hint ? `${id}-hint` : undefined} {...rest} />
    </Shell>
  );
}

export function SelectField({ label, hint, error, className, children, ...rest }: FieldShell & SelectHTMLAttributes<HTMLSelectElement>) {
  const id = useId();
  return (
    <Shell id={id} label={label} hint={hint} error={error} className={className}>
      <select id={id} className={cn(control, "h-9")} aria-invalid={Boolean(error) || undefined}
        aria-describedby={error ? `${id}-err` : hint ? `${id}-hint` : undefined} {...rest}>
        {children}
      </select>
    </Shell>
  );
}

/**
 * The one switch visual every switch in the app uses. The knob contrasts with the track in both states
 * (muted knob on a neutral track when off, accent-fg knob on accent when on), so state never relies on
 * a pale-on-pale knob.
 */
export function SwitchTrack({ checked }: { checked: boolean }) {
  return (
    <span aria-hidden className={cn("relative inline-block h-5 w-9 shrink-0 rounded-full border transition-colors duration-fast",
      checked ? "bg-accent border-accent" : "bg-surface-3 border-border-strong")}>
      <span className={cn("absolute left-0 top-0.5 size-3.5 rounded-full shadow-1 transition-transform duration-fast",
        checked ? "translate-x-[1.125rem] bg-accent-fg" : "translate-x-0.5 bg-fg-muted")} />
    </span>
  );
}

/**
 * Hit area for a button that wraps a `SwitchTrack`: the 20px track sits centred in a 24px (desktop) or
 * 44px (touch) target so switches meet WCAG 2.5.8. Use it on every switch button.
 */
export const switchHit =
  "inline-flex items-center justify-center min-h-6 rounded-full disabled:opacity-50 disabled:cursor-not-allowed " +
  "[@media(pointer:coarse)]:min-h-11 [@media(pointer:coarse)]:min-w-11";

export function Toggle({ label, checked, onChange, hint, disabled }: { label: string; checked: boolean; onChange: (v: boolean) => void; hint?: string; disabled?: boolean }) {
  const id = useId();
  return (
    <div className="flex items-start gap-3">
      <button id={id} type="button" role="switch" aria-checked={checked} disabled={disabled}
        aria-describedby={hint ? `${id}-hint` : undefined} onClick={() => onChange(!checked)}
        className={cn(switchHit, "-my-0.5 [@media(pointer:coarse)]:-my-3")}>
        <SwitchTrack checked={checked} />
      </button>
      <div className="text-sm">
        <label htmlFor={id} className="font-medium cursor-pointer">{label}</label>
        {hint && <p id={`${id}-hint`} className="text-xs text-fg-subtle">{hint}</p>}
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ Segmented control
type SegSize = "sm" | "md";
type SegOption<T extends string> = { value: T; label: string; hint?: string; icon?: LucideIcon };

/**
 * Shared look of every segmented control (radios and tabs). The selected segment carries a 1px accent
 * ring, which keeps at least 3:1 against both the track and the segment in light and dark (WCAG 1.4.11);
 * in dark mode it is also raised to surface-3 so it reads lighter than the track, never darker.
 */
const segTrack = "inline-flex max-w-full gap-1 rounded-lg bg-surface-2 p-1";
const segSizes: Record<SegSize, string> = { sm: "h-7 px-2.5 text-xs gap-1", md: "h-8 px-3 text-sm gap-1.5" };
const segItem = (on: boolean, size: SegSize) =>
  cn("inline-flex items-center justify-center rounded-md whitespace-nowrap transition-colors duration-fast", segSizes[size], touchTarget,
    on ? "bg-surface dark:bg-surface-3 text-fg font-medium shadow-1 ring-1 ring-inset ring-accent" : "text-fg-muted hover:text-fg hover:bg-surface-3/60");

/** Arrow/Home/End handling shared by radiogroups and tablists (roving tabindex: only the current item is tabbable). */
function useRoving<T extends string>(options: { value: T }[], value: T, onChange: (v: T) => void) {
  const refs = useRef<(HTMLButtonElement | null)[]>([]);
  const onKeyDown = (e: KeyboardEvent<HTMLButtonElement>) => {
    const n = options.length;
    const i = Math.max(0, options.findIndex((o) => o.value === value));
    let next = -1;
    if (e.key === "ArrowRight" || e.key === "ArrowDown") next = (i + 1) % n;
    else if (e.key === "ArrowLeft" || e.key === "ArrowUp") next = (i - 1 + n) % n;
    else if (e.key === "Home") next = 0;
    else if (e.key === "End") next = n - 1;
    if (next < 0) return;
    e.preventDefault();
    onChange(options[next]!.value);
    refs.current[next]?.focus();
  };
  const hasValue = options.some((o) => o.value === value);
  const tabIndex = (o: { value: T }, i: number) => (o.value === value || (!hasValue && i === 0) ? 0 : -1);
  return { refs, onKeyDown, tabIndex };
}

export function Segmented<T extends string>({ label, value, onChange, options, size = "md", hideLabel, className }: {
  label: string;
  value: T;
  onChange: (v: T) => void;
  options: SegOption<T>[];
  size?: SegSize;
  /** Keep the legend for assistive tech only (for toolbars where context already names the control). */
  hideLabel?: boolean;
  className?: string;
}) {
  const { refs, onKeyDown, tabIndex } = useRoving(options, value, onChange);
  return (
    <fieldset className={cn("flex flex-col gap-1 min-w-0", className)}>
      <legend className={cn(hideLabel ? "sr-only" : "text-sm font-medium mb-1")}>{label}</legend>
      <div role="radiogroup" aria-label={label} className={cn(segTrack, "flex-wrap self-start")}>
        {options.map((o, i) => (
          <button key={o.value} ref={(el) => { refs.current[i] = el; }} type="button" role="radio" aria-checked={value === o.value}
            title={o.hint} tabIndex={tabIndex(o, i)} onKeyDown={onKeyDown}
            onClick={() => onChange(o.value)} className={segItem(value === o.value, size)}>
            {o.icon && <o.icon className={size === "sm" ? "size-3.5" : "size-4"} aria-hidden />}
            {o.label}
          </button>
        ))}
      </div>
    </fieldset>
  );
}

/**
 * The segmented look as a tablist (for view switchers). Each tab gets the id `${idPrefix}-tab-${value}` and
 * controls `panelId`; give the panel `role="tabpanel"` and `aria-labelledby` that tab id.
 */
export function SegmentedTabs<T extends string>({ label, value, onChange, options, idPrefix, panelId, size = "sm", className }: {
  label: string;
  value: T;
  onChange: (v: T) => void;
  options: SegOption<T>[];
  idPrefix: string;
  panelId: string;
  size?: SegSize;
  className?: string;
}) {
  const { refs, onKeyDown, tabIndex } = useRoving(options, value, onChange);
  return (
    <div role="tablist" aria-label={label} className={cn(segTrack, "overflow-x-auto", className)}>
      {options.map((o, i) => (
        <button key={o.value} ref={(el) => { refs.current[i] = el; }} type="button" role="tab" id={`${idPrefix}-tab-${o.value}`}
          aria-selected={value === o.value} aria-controls={panelId} title={o.hint} tabIndex={tabIndex(o, i)}
          onKeyDown={onKeyDown} onClick={() => onChange(o.value)} className={segItem(value === o.value, size)}>
          {o.icon && <o.icon className={size === "sm" ? "size-3.5" : "size-4"} aria-hidden />}
          {o.label}
        </button>
      ))}
    </div>
  );
}

// ------------------------------------------------------------------ Stepper
/** Bounded integer stepper (council size, seat counts). Promoted from components/councils/fields. */
export function Stepper({ label, value, min, max, onChange, unit = "seats", hint, error, disabled }: {
  label: string;
  value: number;
  min: number;
  max: number;
  onChange: (v: number) => void;
  unit?: string;
  hint?: ReactNode;
  error?: string;
  disabled?: boolean;
}) {
  const id = useId();
  const atMin = value <= min;
  const atMax = value >= max;
  const btn = cn("inline-flex items-center justify-center h-9 w-9 text-fg-muted hover:bg-surface-2 hover:text-fg",
    "disabled:opacity-40 disabled:cursor-not-allowed", touchSquare);
  return (
    <div className="flex flex-col gap-1">
      <span id={`${id}-label`} className="text-sm font-medium">{label}</span>
      <div role="group" aria-labelledby={`${id}-label`} aria-describedby={error ? `${id}-err` : hint ? `${id}-hint` : undefined}
        className={cn("inline-flex items-center self-start rounded-md border bg-surface", error ? "border-danger" : "border-border-strong")}>
        <button type="button" aria-label={`Fewer ${unit}`} disabled={disabled || atMin}
          title={atMin ? `Minimum is ${min}` : `Fewer ${unit}`} className={cn(btn, "rounded-l-md")}
          onClick={() => onChange(Math.max(min, value - 1))}>
          <Minus className="size-4" aria-hidden />
        </button>
        <output aria-live="polite" className="min-w-12 px-1 text-center tabular text-base font-medium border-x border-border">
          {value}
        </output>
        <button type="button" aria-label={`More ${unit}`} disabled={disabled || atMax}
          title={atMax ? `Maximum is ${max}` : `More ${unit}`} className={cn(btn, "rounded-r-md")}
          onClick={() => onChange(Math.min(max, value + 1))}>
          <Plus className="size-4" aria-hidden />
        </button>
      </div>
      {error ? <p id={`${id}-err`} className="text-xs text-danger" role="alert">{error}</p>
        : hint ? <p id={`${id}-hint`} className="text-xs text-fg-subtle">{hint}</p> : null}
    </div>
  );
}

// ------------------------------------------------------------------ Status
export const STATUS: Record<string, { tone: Tone; icon: LucideIcon; label?: string; spin?: boolean }> = {
  queued: { tone: "neutral", icon: CircleDashed },
  planning: { tone: "info", icon: Loader2, spin: true },
  running: { tone: "info", icon: Loader2, spin: true },
  paused: { tone: "warning", icon: PauseCircle },
  awaiting_approval: { tone: "warning", icon: AlertTriangle, label: "awaiting approval" },
  completed: { tone: "success", icon: CheckCircle2 },
  failed: { tone: "danger", icon: XCircle },
  halted: { tone: "danger", icon: ShieldX },
  cancelled: { tone: "neutral", icon: XCircle },
};

type Tone = "neutral" | "info" | "success" | "warning" | "danger" | "accent";
const tones: Record<Tone, string> = {
  neutral: "bg-neutral-subtle text-neutral",
  info: "bg-info-subtle text-info",
  success: "bg-success-subtle text-success",
  warning: "bg-warning-subtle text-warning",
  danger: "bg-danger-subtle text-danger",
  accent: "bg-accent-subtle text-accent",
};

export function Badge({ tone = "neutral", children, className }: { tone?: Tone; children: ReactNode; className?: string }) {
  return (
    <span className={cn("inline-flex items-center gap-1 rounded-sm px-1.5 h-5 text-2xs uppercase tracking-wide", tones[tone], className)}>
      {children}
    </span>
  );
}

export function StatusPill({ status, className }: { status: string; className?: string }) {
  const s = STATUS[status] ?? { tone: "neutral" as Tone, icon: CircleDashed };
  return (
    <Badge tone={s.tone} className={cn("rounded-full px-2", className)}>
      <s.icon className={cn("size-3", s.spin && "animate-spin")} aria-hidden />
      {s.label ?? status}
    </Badge>
  );
}

// ------------------------------------------------------------------ Surfaces & states
export function Card({ className, children, as: As = "section", ...rest }: { className?: string; children: ReactNode; as?: "section" | "div" | "article"; [k: string]: unknown }) {
  return (
    <As className={cn("rounded-lg border border-border bg-surface shadow-1", className)} {...rest}>
      {children}
    </As>
  );
}

export function Skeleton({ className }: { className?: string }) {
  return <div aria-hidden className={cn("animate-pulse rounded-md bg-surface-3", className)} />;
}

export function EmptyState({ icon: Icon, title, body, action, titleAs: Title = "h2" }: {
  icon?: LucideIcon;
  title: string;
  body?: ReactNode;
  action?: ReactNode;
  /** Heading level of the title. `h2` by default so an empty section still has a heading; `h1` on full-page states (404). */
  titleAs?: "h1" | "h2" | "h3" | "p";
}) {
  return (
    <div className="flex flex-col items-center justify-center text-center gap-2 py-12 px-4">
      {Icon && <Icon className="size-8 text-fg-subtle" aria-hidden />}
      <Title className="text-lg">{title}</Title>
      {body && <div className="text-sm text-fg-muted max-w-md">{body}</div>}
      {action && <div className="mt-2">{action}</div>}
    </div>
  );
}

export function ErrorState({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const e = error instanceof ApiError ? error : null;
  return (
    <div role="alert" className="flex flex-col items-center text-center gap-2 py-10 px-4">
      <XCircle className="size-8 text-danger" aria-hidden />
      <p className="text-lg">{e ? e.message : "Something went wrong"}</p>
      {e && <p className="text-xs text-fg-subtle font-mono">{e.code} · HTTP {e.status}</p>}
      {e && Object.keys(e.details).length > 0 && (
        <details className="text-xs text-fg-muted max-w-lg text-left">
          <summary className="cursor-pointer">Details</summary>
          <pre className="whitespace-pre-wrap font-mono">{JSON.stringify(e.details, null, 2)}</pre>
        </details>
      )}
      {onRetry && <Button onClick={onRetry} size="sm">Retry</Button>}
    </div>
  );
}

export function Callout({ tone = "info", title, children, action }: { tone?: "info" | "warning" | "danger" | "success"; title: string; children?: ReactNode; action?: ReactNode }) {
  const Icon = tone === "danger" ? XCircle : tone === "warning" ? AlertTriangle : tone === "success" ? CheckCircle2 : Info;
  return (
    <div role={tone === "danger" ? "alert" : "status"} className={cn("flex gap-3 rounded-lg p-3 border", tones[tone], "border-border")}>
      <Icon className="size-5 shrink-0 mt-0.5" aria-hidden />
      <div className="flex-1 text-sm text-fg">
        <p className="font-semibold">{title}</p>
        {children && <div className="text-fg-muted mt-0.5">{children}</div>}
      </div>
      {action}
    </div>
  );
}

// ------------------------------------------------------------------ Page frame
type PageWidth = "form" | "default" | "wide";
const pageWidths: Record<PageWidth, string> = { form: "max-w-[880px]", default: "max-w-6xl", wide: "max-w-none" };

/**
 * The one page container (C-09). `form` is for single-column forms (Home, builder, profile-like editors),
 * `default` for lists, tables and detail pages, `wide` for dashboards that use the full canvas.
 */
export function Page({ width = "default", className, children, as: As = "div", ...rest }: {
  width?: PageWidth;
  className?: string;
  children: ReactNode;
  as?: ElementType;
  [k: string]: unknown;
}) {
  return (
    <As className={cn("w-full min-w-0 mx-auto p-4 md:p-6", pageWidths[width], className)} {...rest}>
      {children}
    </As>
  );
}

/**
 * The page-header contract (C-12): optional back link and eyebrow, the page's single h1 with an optional
 * inline badge, a description capped at a readable measure, a meta row of badges, and actions that keep
 * their own right-hand column from `sm` up however long the description is.
 */
export function PageHeader({ title, description, actions, eyebrow, meta, badge, back, className }: {
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  eyebrow?: ReactNode;
  meta?: ReactNode;
  /** Shown inline after the title (for example a mode badge). */
  badge?: ReactNode;
  back?: { to: string; label: string };
  className?: string;
}) {
  return (
    <div className={cn("mb-6", className)}>
      {back && (
        <Link to={back.to} className="inline-flex items-center gap-1 text-sm text-fg-muted hover:text-fg mb-2 min-h-6">
          <ArrowLeft className="size-4" aria-hidden />{back.label}
        </Link>
      )}
      {eyebrow && <p className="text-xs font-medium uppercase tracking-wide text-fg-subtle mb-1">{eyebrow}</p>}
      <div className="flex flex-col gap-3 sm:flex-row sm:flex-nowrap sm:items-start sm:justify-between">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <h1 className="text-xl break-words min-w-0">{title}</h1>
            {badge}
          </div>
          {description && <div className="text-sm text-fg-muted mt-1 max-w-prose">{description}</div>}
          {meta && <div className="mt-2 flex flex-wrap items-center gap-1.5">{meta}</div>}
        </div>
        {actions && <div className="flex flex-wrap items-center gap-2 sm:justify-end sm:max-w-[60%]">{actions}</div>}
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ Section navigation
export interface SectionLink {
  to: string;
  label: string;
  icon?: LucideIcon;
  /** Match the path exactly (default true). */
  end?: boolean;
}

/**
 * Sub-navigation for a section (Connections, Settings, Insights): links styled as tabs, the current one
 * marked `aria-current="page"` by NavLink. It sits under the page header (eyebrow → h1 → tabs → content).
 */
export function SectionNav({ label, links, className }: { label: string; links: SectionLink[]; className?: string }) {
  return (
    <nav aria-label={label} className={cn("mb-6 -mx-4 md:mx-0 border-b border-border overflow-x-auto", className)}>
      <ul className="flex gap-1 px-4 md:px-0 min-w-max">
        {links.map((l) => (
          <li key={l.to}>
            <NavLink to={l.to} end={l.end ?? true}
              className={({ isActive }) => cn(
                "inline-flex items-center gap-1.5 h-10 px-3 -mb-px text-sm border-b-2 whitespace-nowrap transition-colors duration-fast",
                touchTarget,
                isActive ? "border-accent text-fg font-medium" : "border-transparent text-fg-muted hover:text-fg hover:border-border-strong",
              )}>
              {l.icon && <l.icon className="size-4" aria-hidden />}
              {l.label}
            </NavLink>
          </li>
        ))}
      </ul>
    </nav>
  );
}

// ------------------------------------------------------------------ Dialog
export function Dialog({ open, onOpenChange, title, description, children, footer, wide }: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description?: string;
  children: ReactNode;
  footer?: ReactNode;
  wide?: boolean;
}) {
  return (
    <RDialog.Root open={open} onOpenChange={onOpenChange}>
      <RDialog.Portal>
        <RDialog.Overlay className="fixed inset-0 bg-black/40 z-dialog" />
        <RDialog.Content
          className={cn("fixed left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 z-dialog w-[calc(100vw-2rem)] max-h-[90vh] overflow-auto",
            "rounded-xl bg-surface border border-border shadow-3 p-5", wide ? "max-w-3xl" : "max-w-lg")}>
          <div className="flex items-start justify-between gap-4 mb-3">
            <div>
              <RDialog.Title className="text-lg">{title}</RDialog.Title>
              {description ? (
                <RDialog.Description className="text-sm text-fg-muted mt-1">{description}</RDialog.Description>
              ) : (
                <RDialog.Description className="sr-only">{title}</RDialog.Description>
              )}
            </div>
            <RDialog.Close asChild>
              <IconButton label="Close" icon={X} />
            </RDialog.Close>
          </div>
          {children}
          {footer && <div className="flex justify-end gap-2 mt-5">{footer}</div>}
        </RDialog.Content>
      </RDialog.Portal>
    </RDialog.Root>
  );
}

// ------------------------------------------------------------------ Tabs
export function Tabs({ value, onValueChange, tabs, className }: {
  value: string;
  onValueChange: (v: string) => void;
  tabs: { value: string; label: ReactNode; content: ReactNode }[];
  className?: string;
}) {
  return (
    <RTabs.Root value={value} onValueChange={onValueChange} className={cn("flex flex-col min-h-0", className)}>
      <RTabs.List className="flex gap-1 border-b border-border px-2 overflow-x-auto shrink-0" aria-label="Sections">
        {tabs.map((t) => (
          <RTabs.Trigger key={t.value} value={t.value}
            className={cn("h-9 px-2.5 text-sm text-fg-muted border-b-2 border-transparent data-[state=active]:border-accent data-[state=active]:text-fg whitespace-nowrap", touchTarget)}>
            {t.label}
          </RTabs.Trigger>
        ))}
      </RTabs.List>
      {tabs.map((t) => (
        <RTabs.Content key={t.value} value={t.value} className="flex-1 min-h-0 overflow-auto focus-visible:outline-none">
          {t.content}
        </RTabs.Content>
      ))}
    </RTabs.Root>
  );
}

// ------------------------------------------------------------------ Toasts
export function Toaster() {
  const { toasts, dismiss } = useToasts();
  return (
    <div aria-live="polite" className="fixed bottom-4 right-4 z-toast flex flex-col gap-2 w-[min(24rem,calc(100vw-2rem))]">
      {toasts.map((t) => (
        <div key={t.id} role={t.tone === "danger" ? "alert" : "status"}
          className="animate-enter rounded-lg border border-border bg-surface shadow-2 p-3 flex gap-3">
          <div className={cn("w-1 rounded-full", { info: "bg-info", success: "bg-success", warning: "bg-warning", danger: "bg-danger" }[t.tone])} />
          <div className="flex-1 text-sm">
            <p className="font-semibold">{t.title}</p>
            {t.body && <p className="text-fg-muted">{t.body}</p>}
          </div>
          {t.action && <Button size="sm" onClick={t.action.run}>{t.action.label}</Button>}
          <IconButton label="Dismiss" icon={X} onClick={() => dismiss(t.id)} />
        </div>
      ))}
    </div>
  );
}

export function Kbd({ children }: { children: ReactNode }) {
  return <kbd className="rounded-sm border border-border bg-surface-2 px-1 font-mono text-2xs text-fg-muted">{children}</kbd>;
}
