// Design-system primitives (ui-spec §7.6). Small, accessible, token-driven.

import * as RDialog from "@radix-ui/react-dialog";
import * as RTabs from "@radix-ui/react-tabs";
import {
  AlertTriangle,
  CheckCircle2,
  CircleDashed,
  Info,
  Loader2,
  PauseCircle,
  ShieldX,
  X,
  XCircle,
  type LucideIcon,
} from "lucide-react";
import { forwardRef, useId, type ButtonHTMLAttributes, type InputHTMLAttributes, type ReactNode, type SelectHTMLAttributes, type TextareaHTMLAttributes } from "react";

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
      className={cn("px-0 aspect-square", className)} {...rest} />
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

const control =
  "w-full rounded-md border border-border-strong bg-surface px-3 text-base text-fg placeholder:text-fg-subtle " +
  "disabled:opacity-60 aria-[invalid=true]:border-danger";

export function TextField({ label, hint, error, className, ...rest }: FieldShell & InputHTMLAttributes<HTMLInputElement>) {
  const id = useId();
  return (
    <Shell id={id} label={label} hint={hint} error={error} className={className}>
      <input id={id} className={cn(control, "h-9")} aria-invalid={Boolean(error) || undefined}
        aria-describedby={error ? `${id}-err` : hint ? `${id}-hint` : undefined} {...rest} />
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

export function Toggle({ label, checked, onChange, hint, disabled }: { label: string; checked: boolean; onChange: (v: boolean) => void; hint?: string; disabled?: boolean }) {
  const id = useId();
  return (
    <div className="flex items-start gap-3">
      <button id={id} type="button" role="switch" aria-checked={checked} disabled={disabled}
        aria-describedby={hint ? `${id}-hint` : undefined} onClick={() => onChange(!checked)}
        className="mt-0.5 inline-flex rounded-full disabled:opacity-50 disabled:cursor-not-allowed">
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
export function Segmented<T extends string>({ label, value, onChange, options }: {
  label: string;
  value: T;
  onChange: (v: T) => void;
  options: { value: T; label: string; hint?: string; icon?: LucideIcon }[];
}) {
  return (
    <fieldset className="flex flex-col gap-1">
      <legend className="text-sm font-medium mb-1">{label}</legend>
      <div role="radiogroup" aria-label={label} className="inline-flex flex-wrap gap-1 rounded-lg bg-surface-2 p-1">
        {options.map((o) => (
          <button key={o.value} type="button" role="radio" aria-checked={value === o.value} title={o.hint}
            onClick={() => onChange(o.value)}
            className={cn("inline-flex items-center gap-1.5 rounded-md px-3 h-8 text-sm transition-colors duration-fast",
              value === o.value ? "bg-surface text-fg shadow-1 font-medium" : "text-fg-muted hover:text-fg")}>
            {o.icon && <o.icon className="size-4" aria-hidden />}
            {o.label}
          </button>
        ))}
      </div>
    </fieldset>
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

export function EmptyState({ icon: Icon, title, body, action }: { icon?: LucideIcon; title: string; body?: ReactNode; action?: ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center text-center gap-2 py-12 px-4">
      {Icon && <Icon className="size-8 text-fg-subtle" aria-hidden />}
      <p className="text-lg">{title}</p>
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

export function PageHeader({ title, description, actions }: { title: string; description?: ReactNode; actions?: ReactNode }) {
  return (
    <div className="flex flex-wrap items-end justify-between gap-3 mb-6">
      <div>
        <h1 className="text-xl">{title}</h1>
        {description && <p className="text-sm text-fg-muted mt-1">{description}</p>}
      </div>
      {actions && <div className="flex gap-2">{actions}</div>}
    </div>
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
            className="h-9 px-2.5 text-sm text-fg-muted border-b-2 border-transparent data-[state=active]:border-accent data-[state=active]:text-fg whitespace-nowrap">
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
