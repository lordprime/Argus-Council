// The one rendering of a run or council mode (audit C-13): title-case label plus its icon, inline in
// metadata lines and table cells, or as a Badge in headers and cards. The icon is decorative; the
// label carries the meaning, so screen readers hear the mode once.

import { Code2, FileText, MessageCircle, Wand2, type LucideIcon } from "lucide-react";

import { Badge } from "@/components/ui";
import { cn } from "@/lib/cn";
import type { Mode } from "@/lib/types";

export const MODE_META: Record<Mode, { label: string; icon: LucideIcon }> = {
  research: { label: "Research", icon: FileText },
  coding: { label: "Coding", icon: Code2 },
  chat: { label: "Chat", icon: MessageCircle },
  general: { label: "General", icon: Wand2 },
};

export function modeMeta(mode: string): { label: string; icon: LucideIcon } {
  return MODE_META[mode as Mode] ?? { label: mode ? mode[0]!.toUpperCase() + mode.slice(1) : "General", icon: Wand2 };
}

export function ModeLabel({ mode, variant = "inline", className }: { mode: string; variant?: "inline" | "badge"; className?: string }) {
  const m = modeMeta(mode);
  if (variant === "badge") {
    return (
      <Badge className={className}>
        <m.icon className="size-3" aria-hidden />
        {m.label}
      </Badge>
    );
  }
  return (
    <span className={cn("inline-flex items-center gap-1.5 whitespace-nowrap", className)}>
      <m.icon className="size-[1.1em] shrink-0 text-fg-subtle" aria-hidden />
      {m.label}
    </span>
  );
}
