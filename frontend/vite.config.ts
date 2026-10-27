/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";

const api = process.env.ARGUS_API_URL ?? "http://localhost:8000";
const proxy = Object.fromEntries(
  ["/api", "/health", "/mcp", "/a2a", "/acp", "/.well-known"].map((p) => [p, { target: api, changeOrigin: true }]),
);

export default defineConfig({
  plugins: [react()],
  resolve: { alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) } },
  server: { port: 5173, proxy },
  // `vite preview` serves the production build with the same proxy (used for local e2e runs).
  preview: { port: 4173, proxy },
  // No inlined data: assets, so the strict CSP (font-src 'self') holds.
  build: { sourcemap: false, chunkSizeWarningLimit: 600, assetsInlineLimit: 0 },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    css: false,
    // axe accessibility checks take several seconds each; the 5s default flakes on loaded machines.
    testTimeout: 20_000,
  },
});
