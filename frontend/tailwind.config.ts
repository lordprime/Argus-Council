import type { Config } from "tailwindcss";

const token = (name: string) => `rgb(var(--${name}) / <alpha-value>)`;

export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  darkMode: ["selector", '[data-theme="dark"]'],
  theme: {
    extend: {
      colors: {
        canvas: token("canvas"),
        surface: { DEFAULT: token("surface"), 2: token("surface-2"), 3: token("surface-3") },
        border: { DEFAULT: token("border"), strong: token("border-strong") },
        fg: { DEFAULT: token("text"), muted: token("text-muted"), subtle: token("text-subtle") },
        accent: { DEFAULT: token("accent"), hover: token("accent-hover"), fg: token("accent-fg"), subtle: token("accent-subtle") },
        success: { DEFAULT: token("success"), subtle: token("success-subtle") },
        warning: { DEFAULT: token("warning"), subtle: token("warning-subtle") },
        danger: { DEFAULT: token("danger"), fg: token("danger-fg"), subtle: token("danger-subtle") },
        info: { DEFAULT: token("info"), subtle: token("info-subtle") },
        neutral: { DEFAULT: token("neutral"), subtle: token("neutral-subtle") },
      },
      fontFamily: { sans: ["var(--font-sans)"], display: ["var(--font-display)"], mono: ["var(--font-mono)"] },
      fontSize: {
        "2xs": ["0.6875rem", { lineHeight: "1rem", fontWeight: "500" }],
        xs: ["0.75rem", "1rem"],
        sm: ["0.8125rem", "1.25rem"],
        base: ["0.875rem", "1.375rem"],
        md: ["1rem", "1.625rem"],
        /* lg/xl/2xl/3xl are set in the display family (SectionTitle, PageHeader h1, card display
           moments); tighter negative tracking reads intentional on a serif rather than loose default. */
        lg: ["1.125rem", { lineHeight: "1.5rem", fontWeight: "600", letterSpacing: "-0.006em" }],
        xl: ["1.375rem", { lineHeight: "1.75rem", fontWeight: "600", letterSpacing: "-0.01em" }],
        "2xl": ["1.75rem", { lineHeight: "2.125rem", fontWeight: "600", letterSpacing: "-0.014em" }],
        "3xl": ["2.25rem", { lineHeight: "2.625rem", fontWeight: "600", letterSpacing: "-0.018em" }],
      },
      borderRadius: { sm: "3px", md: "5px", lg: "7px", xl: "10px" },
      boxShadow: { 1: "var(--shadow-1)", 2: "var(--shadow-2)", 3: "var(--shadow-3)" },
      zIndex: { sticky: "10", rail: "20", header: "30", drawer: "40", popover: "50", dialog: "60", toast: "70", palette: "80" },
      transitionDuration: { fast: "var(--dur-fast)", base: "var(--dur-base)", slow: "var(--dur-slow)" },
    },
  },
  plugins: [],
} satisfies Config;
