// ADRs link to one another as files (`0125-a-state-is-a-field.md`) and to the code as
// relative paths (`../../plugins/lots`), so they read right on GitHub. Rendered here, an
// ADR's link goes to that ADR's page, and a path goes to GitHub at main.
import { posix } from "node:path";
import { defineMdastPlugin, type MdastPluginDefinition } from "satteri";
import { githubUrl } from "./repo.ts";

const SCHEME = /^[a-z][a-z0-9+.-]*:/i;

export function rewrite(url: string): string {
  if (SCHEME.test(url) || url.startsWith("#") || url.startsWith("//") || url === "") return url;
  const [, path = url, rest = ""] = /^([^?#]*)(.*)$/.exec(url) ?? [];
  const adr = /^(?:\.\/)?(\d{4})-[^/]*\.md$/.exec(path);
  if (adr) return `/adrs/${adr[1]}/${rest}`;
  const file = posix.normalize(posix.join("docs/adr", path));
  // A path the repository doesn't have fails the build here, as a page's citation does.
  return githubUrl(file) + rest;
}

export const adrLinks: MdastPluginDefinition = defineMdastPlugin({
  name: "iki-architecture-adr-links",
  link(node, context) {
    context.setField(node, "url", rewrite(node.url));
  },
  definition(node, context) {
    context.setField(node, "url", rewrite(node.url));
  },
});
