import { create } from "zustand";

export type Theme = "system" | "light" | "dark";
export type Density = "comfortable" | "compact";
export type Palette = "wine" | "indigo" | "graphite" | "forest" | "ocean" | "ember";

/** Colour palettes (tokens.css). `swatch` is [paper, accent] in light mode, for the picker. */
export const PALETTES: { v: Palette; label: string; hint: string; swatch: [string, string] }[] = [
  { v: "wine", label: "Wine", hint: "Warm paper, deep wine", swatch: ["rgb(247 244 238)", "rgb(122 32 72)"] },
  { v: "indigo", label: "Indigo", hint: "The original Argus look", swatch: ["rgb(246 247 249)", "rgb(67 56 202)"] },
  { v: "graphite", label: "Graphite", hint: "Monochrome ink", swatch: ["rgb(246 246 245)", "rgb(32 32 32)"] },
  { v: "forest", label: "Forest", hint: "Sage paper, deep green", swatch: ["rgb(244 245 240)", "rgb(34 94 64)"] },
  { v: "ocean", label: "Ocean", hint: "Cool slate, teal", swatch: ["rgb(243 246 248)", "rgb(12 100 112)"] },
  { v: "ember", label: "Ember", hint: "Warm sand, burnt orange", swatch: ["rgb(249 245 239)", "rgb(160 60 22)"] },
];

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

export function applyPalette(palette: Palette) {
  if (palette === "wine") delete document.documentElement.dataset.palette;
  else document.documentElement.dataset.palette = palette;
}

const readPalette = (): Palette => {
  const v = read<string>("argus.palette", "wine");
  return PALETTES.some((p) => p.v === v) ? (v as Palette) : "wine";
};

interface UiState {
  theme: Theme;
  palette: Palette;
  density: Density;
  navCollapsed: boolean;
  shortcuts: boolean;
  setTheme: (t: Theme) => void;
  setPalette: (p: Palette) => void;
  setDensity: (d: Density) => void;
  toggleNav: () => void;
  setShortcuts: (on: boolean) => void;
}

export const useUi = create<UiState>((set) => ({
  theme: read<Theme>("argus.theme", "system"),
  palette: readPalette(),
  density: read<Density>("argus.density", "comfortable"),
  navCollapsed: read<string>("argus.nav", "open") === "collapsed",
  shortcuts: read("argus.shortcuts", "on") === "on",
  setTheme: (theme) => {
    write("argus.theme", theme);
    applyTheme(theme);
    set({ theme });
  },
  setPalette: (palette) => {
    write("argus.palette", palette);
    applyPalette(palette);
    set({ palette });
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
