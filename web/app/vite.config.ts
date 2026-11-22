/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defaultClientConditions, defineConfig } from "vite";

// The server serves the built app: its page template loads the entry chunk
// that `manifest.json` names, from the server's static files. (Not Vite's
// default `.vite/manifest.json`: collectstatic skips dot-directories.) Styles
// aren't bundled: the app uses the server's stylesheet (static/iki/iki.css),
// so its pages and the server-rendered ones share one design.
const OUT_DIR = "../../server/src/iki_server/web/static/app";

// `pnpm dev`: Vite serves the app's modules and proxies everything else
// (pages, /api, /static, sign-in) to the server, by default the dev
// cluster's port-forward. Sign in there first; cookies are shared by port.
const SERVER = process.env["IKI_DEV_SERVER"] ?? "http://localhost:8000";

export default defineConfig({
  base: "/static/app/",
  plugins: [react()],
  // Workspace packages (the SDK) are bundled from their sources.
  resolve: { conditions: ["@ikisci/source", ...defaultClientConditions] },
  build: {
    outDir: OUT_DIR,
    emptyOutDir: true,
    manifest: "manifest.json",
    sourcemap: true,
    target: "es2023",
    // The app, and search everything's palette alone, for the server's own pages (ADR 0176).
    rolldownOptions: { input: ["src/main.tsx", "src/search-entry.tsx"] },
  },
  // Workers (the parsers' Web Worker, ADR 0088) are named apart from the app's other
  // chunks, so the server can serve them with their own Content Security Policy.
  worker: {
    format: "es",
    rolldownOptions: { output: { entryFileNames: "assets/worker-[name]-[hash].js" } },
  },
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      "^/(?!static/app/).*": {
        target: SERVER,
        // Tell the server to load the app from this dev server instead.
        headers: { "X-Iki-Web-Dev": "1" },
      },
    },
  },
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.{ts,tsx}"],
    setupFiles: ["src/test-setup.ts"],
    // A test that types a whole form, key by key, takes seconds on a busy machine: more
    // than the default five. One that hangs still fails.
    testTimeout: 20_000,
  },
});
