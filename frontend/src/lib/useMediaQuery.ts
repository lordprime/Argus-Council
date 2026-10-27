// Subscribes to a CSS media query. Used where a layout must render different components (not just
// restyle the same ones) at a breakpoint, such as the run inspector's inline pane versus its drawer.

import { useCallback, useSyncExternalStore } from "react";

function query(q: string): MediaQueryList | null {
  try {
    return typeof window !== "undefined" && typeof window.matchMedia === "function" ? window.matchMedia(q) : null;
  } catch {
    return null;
  }
}

export function useMediaQuery(q: string): boolean {
  const subscribe = useCallback((notify: () => void) => {
    const mql = query(q);
    if (!mql?.addEventListener) return () => {};
    mql.addEventListener("change", notify);
    return () => mql.removeEventListener("change", notify);
  }, [q]);
  return useSyncExternalStore(subscribe, () => query(q)?.matches ?? false, () => false);
}

/** Tailwind's default breakpoints (tailwind.config.ts keeps them): sm 640, md 768, lg 1024, xl 1280. */
export const BREAKPOINT = {
  sm: "(min-width: 640px)",
  md: "(min-width: 768px)",
  lg: "(min-width: 1024px)",
  xl: "(min-width: 1280px)",
} as const;
