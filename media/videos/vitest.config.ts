// The design system's and scripts' tests: pure TypeScript, in Node. Kept apart from
// vite.config.ts, whose Motion Canvas plugin is for rendering.
import { defineConfig } from "vitest/config";

export default defineConfig({ test: { include: ["tests/**/*.test.ts"], environment: "node" } });
