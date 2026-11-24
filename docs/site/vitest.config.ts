import { defineConfig } from "vitest/config";

export default defineConfig({
  // The repository's root, as the build passes it (src/lib/repo.ts).
  define: { __IKI_ROOT__: "undefined" },
  test: { include: ["src/**/*.test.ts"] },
});
