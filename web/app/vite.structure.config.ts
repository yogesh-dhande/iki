import { defineConfig, defaultClientConditions } from "vite";

// The structure viewer's frame (src/structure-frame): Mol*, built on its own, since
// it runs in a document of its own and shares nothing with the app. The server's
// frame page loads the entry that `manifest.json` names (iki_server.viewers).
export default defineConfig({
  resolve: { conditions: ["@ikisci/source", ...defaultClientConditions] },
  base: "/static/viewers/",
  build: {
    outDir: "../../server/src/iki_server/web/static/viewers",
    emptyOutDir: true,
    manifest: "manifest.json",
    sourcemap: true,
    target: "es2023",
    // One module: the frame's policy names it alone.
    rolldownOptions: {
      input: "src/structure-frame/main.ts",
      output: { codeSplitting: false },
    },
    chunkSizeWarningLimit: 8000,
  },
});
