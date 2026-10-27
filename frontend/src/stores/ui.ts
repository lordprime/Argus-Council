import { create } from "zustand";

export type Theme = "system" | "light" | "dark";
export type Density = "comfortable" | "compact";

const read = <T extends string>(key: string, fallback: T): T => {
  try {
    return (localStorage.getItem(key) as T) || fallback;
  } catch {
    return fallback;
  }
};
const write = (key: string, value: string) => {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* ignore */
  }
};

export function applyTheme(theme: Theme) {
  const dark = theme === "dark" || (theme === "system" && window.matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.dataset.theme = dark ? "dark" : "light";
}

interface UiState {
  theme: Theme;
  density: Density;
  navCollapsed: boolean;
  shortcuts: boolean;
  setTheme: (t: Theme) => void;
  setDensity: (d: Density) => void;
  toggleNav: () => void;
  setShortcuts: (on: boolean) => void;
}

export const useUi = create<UiState>((set) => ({
  theme: read<Theme>("argus.theme", "system"),
  density: read<Density>("argus.density", "comfortable"),
  navCollapsed: read<string>("argus.nav", "open") === "collapsed",
  shortcuts: read("argus.shortcuts", "on") === "on",
  setTheme: (theme) => {
    write("argus.theme", theme);
    applyTheme(theme);
    set({ theme });
  },
  setDensity: (density) => {
    write("argus.density", density);
    document.documentElement.dataset.density = density;
    set({ density });
  },
  toggleNav: () =>
    set((s) => {
      write("argus.nav", s.navCollapsed ? "open" : "collapsed");
      return { navCollapsed: !s.navCollapsed };
    }),
  setShortcuts: (on) => {
    write("argus.shortcuts", on ? "on" : "off");
    set({ shortcuts: on });
  },
}));
