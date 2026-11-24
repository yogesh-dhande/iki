// The internal architecture site: built and served on this machine only, never deployed.
import { satteri } from "@astrojs/markdown-satteri";
import { defineConfig } from "astro/config";
import { adrLinks } from "./src/lib/adr-links.ts";

export default defineConfig({
  // Pages link to one another with trailing slashes, as the built directories serve them.
  trailingSlash: "always",
  build: { format: "directory" },
  server: { host: "127.0.0.1", port: 4321 },
  devToolbar: { enabled: false },
  markdown: { processor: satteri({ mdastPlugins: [adrLinks] }) },
});
