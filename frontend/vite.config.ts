/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";

const api = process.env.ARGUS_API_URL ?? "http://localhost:8000";

export default defineConfig({
  plugins: [react()],
  resolve: { alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) } },
  server: {
    port: 5173,
    proxy: Object.fromEntries(
      ["/api", "/health", "/mcp", "/a2a", "/acp", "/.well-known"].map((p) => [p, { target: api, changeOrigin: true }]),
    ),
  },
  // No inlined data: assets, so the strict CSP (font-src 'self') holds.
  build: { sourcemap: false, chunkSizeWarningLimit: 600, assetsInlineLimit: 0 },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    css: false,
  },
});
