// The one rendering of a run or council mode (audit C-13): title-case label plus its icon, inline in
// metadata lines and table cells, or as a Badge in headers and cards. The icon is decorative; the
// label carries the meaning, so screen readers hear the mode once.

import { Badge } from "@/components/ui";
import { cn } from "@/lib/cn";
import { modeMeta } from "@/lib/modes";

// Re-exported so existing imports from this module keep working; the map itself lives in lib/modes.
export { MODE_META, modeMeta } from "@/lib/modes";

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
