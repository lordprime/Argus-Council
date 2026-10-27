import { useSyncExternalStore } from "react";

/** Subscribes to a CSS media query; renders one layout instead of hiding a duplicate with CSS. */
export function useMediaQuery(query: string): boolean {
  return useSyncExternalStore(
    (cb) => {
      const mql = window.matchMedia(query);
      mql.addEventListener?.("change", cb);
      return () => mql.removeEventListener?.("change", cb);
    },
    () => window.matchMedia(query).matches,
    () => false,
  );
}
