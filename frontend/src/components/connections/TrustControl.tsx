import { Ban, ShieldCheck, ShieldQuestion, type LucideIcon } from "lucide-react";
import { useId } from "react";

import { Badge, Callout } from "@/components/ui";
import type { McpTrust } from "@/features/connections/queries";
import { cn } from "@/lib/cn";

export const TRUST: Record<McpTrust, { label: string; icon: LucideIcon; tone: "warning" | "info" | "danger"; body: string }> = {
  ask: {
    label: "Ask",
    icon: ShieldQuestion,
    tone: "info",
    body: "Every tool call from this server pauses the run until you approve it. Recommended for servers you did not build.",
  },
  trusted: {
    label: "Trusted",
    icon: ShieldCheck,
    tone: "warning",
    body: "Tools run without asking first. Guardrails and the tools you mark “always ask” still apply.",
  },
  blocked: {
    label: "Blocked",
    icon: Ban,
    tone: "danger",
    body: "The server stays saved, but its tools are not attached to runs. Connection tests still work.",
  },
};
const ORDER: McpTrust[] = ["ask", "trusted", "blocked"];

/**
 * Trust level as a segmented control built from native radios (arrow keys move between levels). Each level's plain
 * explanation is its accessible description; the chosen one is also shown, with a warning when choosing "trusted".
 */
export function TrustControl({ value, onChange }: { value: McpTrust; onChange: (v: McpTrust) => void }) {
  const id = useId();
  return (
    <fieldset className="flex flex-col gap-2 min-w-0">
      <legend className="text-sm font-medium mb-1">Trust level</legend>
      <div className="grid grid-cols-3 gap-1 rounded-lg bg-surface-2 p-1">
        {ORDER.map((t) => {
          const T = TRUST[t];
          const on = value === t;
          return (
            <label key={t}
              className={cn("relative inline-flex items-center justify-center gap-1.5 rounded-md px-2 h-8 text-sm cursor-pointer transition-colors duration-fast",
                "has-[:focus-visible]:outline has-[:focus-visible]:outline-2 has-[:focus-visible]:outline-accent",
                on ? "bg-surface text-fg shadow-1 font-medium" : "text-fg-muted hover:text-fg")}>
              <input type="radio" name={`${id}-trust`} value={t} checked={on} onChange={() => onChange(t)}
                aria-describedby={`${id}-${t}`} className="sr-only" />
              <T.icon className="size-4 shrink-0" aria-hidden />
              {T.label}
            </label>
          );
        })}
      </div>
      {ORDER.map((t) => (
        <p key={t} id={`${id}-${t}`} className={value === t ? "text-sm text-fg-muted" : "sr-only"}>{TRUST[t].body}</p>
      ))}
      {value === "trusted" && (
        <Callout tone="warning" title="Only trust servers you control">
          A trusted server&apos;s tools run with no confirmation. A compromised or malicious server could read data from the run
          or act on your behalf. Prefer “Ask” and mark only harmless tools as allowed.
        </Callout>
      )}
    </fieldset>
  );
}

export function TrustBadge({ trust }: { trust: McpTrust }) {
  const T = TRUST[trust];
  return (
    <Badge tone={T.tone}>
      <T.icon className="size-3" aria-hidden />
      <span><span className="sr-only">Trust: </span>{T.label}</span>
    </Badge>
  );
}
