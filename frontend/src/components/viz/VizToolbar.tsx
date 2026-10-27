// Shared chrome for the run visualisations (Map, Gantt, Heatmap, Route): one pane wrapper, one toolbar
// (title, meta badges, zoom on the right, filters underneath) and one zoom control with a fixed order:
//   −  ·  %  ·  +  ·  reset  ·  fit
// Every view shows the same parts in the same places, so switching views never moves the controls.

import { CircleDashed, Maximize2, RotateCcw, ZoomIn, ZoomOut } from "lucide-react";
import type { ReactNode } from "react";

import { Badge, IconButton, STATUS } from "@/components/ui";
import { cn } from "@/lib/cn";
import { isTerminal } from "@/stores/run";

/** Padded column that every viz renders into (the workspace pane itself has no padding). */
export function VizPane({ labelledBy, className, children }: { labelledBy: string; className?: string; children: ReactNode }) {
  return (
    <section aria-labelledby={labelledBy} className={cn("flex w-full min-w-0 flex-col gap-3 p-3 md:p-4", className)}>
      {children}
    </section>
  );
}

export function VizToolbar({ titleId, title, meta, zoom, children }: {
  titleId: string;
  title: string;
  /** Badges after the title (status, counts). */
  meta?: ReactNode;
  /** A ZoomControls element; always rendered at the top right. */
  zoom?: ReactNode;
  /** Filters and view options, in one row under the title. */
  children?: ReactNode;
}) {
  return (
    <header className="flex min-w-0 flex-col gap-2">
      <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-2">
        <h2 id={titleId} className="text-md font-semibold text-fg">{title}</h2>
        {meta && <div className="flex min-w-0 flex-wrap items-center gap-1.5">{meta}</div>}
        {zoom && <div className="ml-auto shrink-0">{zoom}</div>}
      </div>
      {children && <div className="flex min-w-0 flex-wrap items-center gap-2">{children}</div>}
    </header>
  );
}

/**
 * "Live" while the run is active, otherwise "Ended · <status>" with the status icon (never colour alone).
 */
export function RunStateBadge({ status, testId }: { status: string; testId?: string }) {
  if (!isTerminal(status)) {
    return (
      <span data-testid={testId} className="inline-flex">
        <Badge tone="success">
          <span aria-hidden className="relative flex size-2">
            <span className="absolute inline-flex size-full rounded-full bg-success opacity-60 motion-safe:animate-ping [[data-motion=reduced]_&]:animate-none" />
            <span className="relative inline-flex size-2 rounded-full bg-success" />
          </span>
          Live
        </Badge>
      </span>
    );
  }
  const s = STATUS[status];
  const Icon = s?.icon ?? CircleDashed;
  return (
    <span data-testid={testId} className="inline-flex">
      <Badge tone={s?.tone === "success" || s?.tone === "danger" ? s.tone : "neutral"}>
        <Icon className="size-3" aria-hidden />
        Ended · {s?.label ?? status}
      </Badge>
    </span>
  );
}

export interface ZoomControlsProps {
  /** Current zoom as a ratio (1 = 100%). */
  value: number;
  min: number;
  max: number;
  onZoomOut: () => void;
  onZoomIn: () => void;
  /** Return to 100% (only where 100% differs from "fit"). */
  onReset?: () => void;
  resetLabel?: string;
  /** Fit everything into the pane. */
  onFit?: () => void;
  fitLabel?: string;
  /** True while the view is already fitted (the fit button then shows as pressed). */
  fitted?: boolean;
  className?: string;
}

/** Zoom control in a fixed order: − / % / + / reset / fit. */
export function ZoomControls({
  value, min, max, onZoomOut, onZoomIn, onReset, resetLabel = "Reset zoom", onFit, fitLabel = "Fit to view", fitted, className,
}: ZoomControlsProps) {
  const pct = Math.round(value * 100);
  // A small tolerance so floating-point drift never disables a button one step too early.
  const atMin = value <= min * 1.001;
  const atMax = value >= max * 0.999;
  return (
    <div role="group" aria-label="Zoom" className={cn("inline-flex items-center gap-0.5", className)}>
      <IconButton label="Zoom out" icon={ZoomOut} onClick={onZoomOut} disabled={atMin} />
      <output aria-label="Zoom level" aria-live="polite" data-testid="zoom-level"
        className="w-12 text-center text-xs tabular-nums text-fg-muted">
        {pct}%
      </output>
      <IconButton label="Zoom in" icon={ZoomIn} onClick={onZoomIn} disabled={atMax} />
      {onReset && <IconButton label={resetLabel} icon={RotateCcw} onClick={onReset} />}
      {onFit && <IconButton label={fitLabel} icon={Maximize2} onClick={onFit} aria-pressed={fitted} />}
    </div>
  );
}
