import { Check } from "lucide-react";

import { cn } from "@/lib/cn";
import { PALETTES, useUi } from "@/stores/ui";

/** Paper with an accent bar: how the palette reads at a glance, whatever theme is showing now. */
export function PaletteSwatch({ swatch, className }: { swatch: [string, string]; className?: string }) {
  const [paper, accent] = swatch;
  return (
    <span aria-hidden className={cn("relative inline-block overflow-hidden rounded-md border border-border-strong/40", className)}
      style={{ background: paper }}>
      <span className="absolute inset-y-0 right-0 w-2/5" style={{ background: accent }} />
    </span>
  );
}

/**
 * Colour palette choice (tokens.css `data-palette`). Native radios, so arrow keys move between
 * palettes and the group has one tab stop; each tile shows its paper and accent.
 */
export function PalettePicker() {
  const { palette, setPalette } = useUi();
  return (
    <fieldset className="flex flex-col gap-2 min-w-0">
      <legend className="text-sm font-medium mb-1">Palette</legend>
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
        {PALETTES.map((p) => {
          const on = palette === p.v;
          return (
            <label key={p.v}
              className={cn("group relative flex items-center gap-3 rounded-lg border p-2.5 pr-8 cursor-pointer transition-colors duration-fast",
                "has-[:focus-visible]:outline has-[:focus-visible]:outline-2 has-[:focus-visible]:outline-offset-2 has-[:focus-visible]:outline-[rgb(var(--focus-ring))]",
                on ? "border-accent bg-accent-subtle/60" : "border-border hover:border-border-strong hover:bg-surface-2")}>
              <input type="radio" name="palette" value={p.v} checked={on} onChange={() => setPalette(p.v)} className="sr-only" />
              <PaletteSwatch swatch={p.swatch} className="size-9 shrink-0" />
              <span className="min-w-0">
                <span className="block text-sm font-medium">{p.label}</span>
                <span className="block text-xs text-fg-subtle truncate">{p.hint}</span>
              </span>
              {on && <Check className="absolute right-2.5 top-2.5 size-4 text-accent" aria-hidden />}
            </label>
          );
        })}
      </div>
    </fieldset>
  );
}
