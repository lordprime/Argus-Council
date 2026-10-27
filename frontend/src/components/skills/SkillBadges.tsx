import { Badge } from "@/components/ui";
import type { SkillSummary } from "@/lib/types";

export function SkillBadges({ skill: s, align = "end" }: { skill: SkillSummary; align?: "start" | "end" }) {
  return (
    <div className={`flex flex-wrap gap-1 shrink-0 ${align === "end" ? "justify-end" : "justify-start"}`}>
      {s.builtin ? <Badge tone="info">Built-in</Badge> : <Badge tone="accent">Yours</Badge>}
      {s.shadows_builtin && <Badge tone="warning">Overrides built-in</Badge>}
      {!s.builtin && !s.enabled && <Badge>Disabled</Badge>}
    </div>
  );
}
