// The council the server planned for a task: every seat with its focus and the model picked for it.

import { Cpu, Gauge, Sparkles, Zap } from "lucide-react";

import { RoleAvatar } from "@/components/ui/role";
import { Badge, Callout } from "@/components/ui";
import type { CouncilPlan, ModelTier } from "@/lib/queries";

const TIER: Record<ModelTier, { label: string; icon: typeof Cpu; tone: "accent" | "neutral" | "info" }> = {
  strong: { label: "Strong", icon: Cpu, tone: "accent" },
  balanced: { label: "Balanced", icon: Gauge, tone: "neutral" },
  fast: { label: "Fast", icon: Zap, tone: "info" },
};

export function CouncilPlanPreview({ plan, stale }: { plan: CouncilPlan; stale?: boolean }) {
  return (
    <section aria-label="Planned council" className="flex flex-col gap-3 rounded-lg border border-border bg-surface-2/50 p-3">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <Sparkles className="size-4 text-accent" aria-hidden />
        <span className="font-medium">{plan.seats.length} seat{plan.seats.length === 1 ? "" : "s"}</span>
        <Badge tone={plan.planner === "llm" ? "accent" : "neutral"}>
          {plan.planner === "llm" ? `Planned by ${plan.planner_route?.model ?? "a model"}` : plan.planner === "heuristic" ? "Planned from keywords" : "Fixed roster"}
        </Badge>
        {stale && <Badge tone="warning">Out of date: preview again</Badge>}
      </div>
      {plan.rationale && <p className="text-sm text-fg-muted">{plan.rationale}</p>}
      <ul className="grid gap-2 sm:grid-cols-2">
        {plan.seats.map((s, i) => {
          const tier = TIER[s.tier] ?? TIER.balanced;
          return (
            <li key={`${s.role}-${i}`} className="flex items-start gap-2.5 rounded-md border border-border bg-surface px-3 py-2">
              <RoleAvatar role={s.role} overseer={s.overseer} size={24} />
              <div className="min-w-0 flex-1">
                <p className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm">
                  <span className="font-medium">{s.name}</span>
                  {s.overseer && <span className="text-xs text-fg-subtle">overseer</span>}
                </p>
                {s.focus && <p className="text-xs text-fg-muted break-words">{s.focus}</p>}
                <p className="mt-1 flex flex-wrap items-center gap-1.5 text-xs">
                  <Badge tone={tier.tone}><tier.icon className="size-3" aria-hidden />{tier.label}</Badge>
                  <span className="font-mono text-fg-subtle truncate" title={`${s.provider} · ${s.model}`}>{s.provider} · {s.model}</span>
                </p>
              </div>
            </li>
          );
        })}
      </ul>
      {plan.notes.length > 0 && (
        <Callout tone="warning" title="Heads up">
          <ul className="list-disc pl-4">{plan.notes.map((n) => <li key={n}>{n}</li>)}</ul>
        </Callout>
      )}
    </section>
  );
}
