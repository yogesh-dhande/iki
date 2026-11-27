// Checks the built architecture site (dist/): every link between its pages goes to a page
// that exists and, with a #fragment, to an element on it; every link to the repository on
// GitHub at main names a file or directory this checkout has. Outside links aren't fetched.
import { execFileSync } from "node:child_process";
import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const dist = path.resolve(here, "../dist");
const repo = path.resolve(here, "../../..");
const GITHUB = "https://github.com/yogesh-dhande/iki/";

if (!existsSync(dist)) {
  console.error("There's no dist/ to check: build the site first (make architecture-build).");
  process.exit(1);
}

function pages(dir) {
  return readdirSync(dir).flatMap((name) => {
    const full = path.join(dir, name);
    if (statSync(full).isDirectory()) return pages(full);
    return name.endsWith(".html") ? [full] : [];
  });
}

const files = new Set(
  execFileSync("git", ["ls-files", "-z", "--cached", "--others", "--exclude-standard"], {
    cwd: repo,
    encoding: "utf8",
    maxBuffer: 256 * 1024 * 1024,
  })
    .split("\0")
    .filter(Boolean),
);
const dirs = new Set();
for (const f of files) {
  const parts = f.split("/");
  for (let i = 1; i < parts.length; i++) dirs.add(parts.slice(0, i).join("/"));
}

const html = new Map(pages(dist).map((p) => [p, readFileSync(p, "utf8")]));
const ids = new Map();
function idsOf(file) {
  if (!ids.has(file)) {
    const text = html.get(file) ?? "";
    ids.set(file, new Set([...text.matchAll(/\sid="([^"]+)"/g)].map((m) => m[1])));
  }
  return ids.get(file);
}

/** The built file a site path is served from, or undefined. */
function target(urlPath) {
  const clean = decodeURIComponent(urlPath);
  const candidates = [path.join(dist, clean), path.join(dist, clean, "index.html")];
  return candidates.find((c) => existsSync(c) && statSync(c).isFile());
}

const problems = [];
let internal = 0;
let github = 0;
let outside = 0;
for (const [file, text] of html) {
  const page = "/" + path.relative(dist, file).replace(/index\.html$/, "");
  for (const m of text.matchAll(/\s(href|src)="([^"]*)"/g)) {
    const url = m[2].replaceAll("&amp;", "&");
    if (url.startsWith(GITHUB)) {
      github += 1;
      const g = /^(blob|tree)\/main\/([^#?]*)/.exec(url.slice(GITHUB.length));
      if (!g) {
        problems.push(`${page}: ${url} isn't a link to a path at main`);
        continue;
      }
      const p = decodeURIComponent(g[2]).replace(/\/$/, "");
      const ok = g[1] === "blob" ? files.has(p) : dirs.has(p);
      if (!ok)
        problems.push(
          `${page}: ${url} names ${p}, which isn't a ${g[1] === "blob" ? "file" : "directory"} here`,
        );
      continue;
    }
    if (/^[a-z][a-z0-9+.-]*:/i.test(url) || url.startsWith("//")) {
      outside += 1;
      continue;
    }
    internal += 1;
    const [pathPart, fragment] = url.split("#");
    let dest = file;
    if (pathPart) {
      if (!pathPart.startsWith("/")) {
        problems.push(`${page}: ${url} is relative; the site's links start with /`);
        continue;
      }
      if (!pathPart.endsWith("/") && !path.extname(pathPart)) {
        problems.push(`${page}: ${url} has no trailing slash`);
      }
      dest = target(pathPart);
      if (!dest) {
        problems.push(`${page}: ${url} goes to no page`);
        continue;
      }
    }
    if (fragment && dest.endsWith(".html") && !idsOf(dest).has(decodeURIComponent(fragment))) {
      problems.push(`${page}: ${url} names #${fragment}, which isn't on that page`);
    }
  }
}

// The plugin browser lists every plugin in the repository (a directory with plugin.yaml),
// each linked to a page that was built, and has a page for every JSON Schema in spec/.
const listPage = path.join(dist, "plugin-browser/all-plugins/index.html");
let browserPlugins = 0;
let unresolved = 0;
if (!html.has(listPage)) {
  problems.push("/plugin-browser/all-plugins/ wasn't built");
} else {
  const rows = new Map(
    [
      ...html
        .get(listPage)
        .matchAll(/<tr[^>]*\sdata-dir="([^"]+)"[^>]*>[\s\S]*?<a href="([^"]+)"/g),
    ].map((m) => [m[1], m[2]]),
  );
  const pluginDirs = [...files]
    .filter((f) => /(^|\/)plugin\.ya?ml$/.test(f))
    .map((f) => f.replace(/\/?plugin\.ya?ml$/, ""));
  for (const dir of new Set(pluginDirs)) {
    const href = rows.get(dir);
    if (!href) problems.push(`/plugin-browser/all-plugins/: the plugin in ${dir} isn't listed`);
    else if (!target(href.split("#")[0]))
      problems.push(`/plugin-browser/all-plugins/: ${dir}'s page ${href} wasn't built`);
  }
  for (const dir of rows.keys()) {
    if (!pluginDirs.includes(dir))
      problems.push(`/plugin-browser/all-plugins/ lists ${dir}, which has no plugin.yaml`);
  }
  browserPlugins = rows.size;
  for (const [file, text] of html) {
    if (file.includes(`${path.sep}plugin-browser${path.sep}`))
      unresolved += (text.match(/class="[^"]*\bunres\b/g) ?? []).length;
  }
}
for (const f of files) {
  const m = /^spec\/src\/iki_spec\/schemas\/([^/]+)\/v1\.json$/.exec(f);
  if (m && !target(`/plugin-browser/schemas/${m[1]}/`)) {
    problems.push(`/plugin-browser/schemas/${m[1]}/ wasn't built for ${f}`);
  }
}

for (const p of problems) console.error(p);
console.log(
  `Plugin browser: ${browserPlugins} plugins listed, each with a page; ${unresolved} unresolved reference(s) marked.`,
);
console.log(
  `${html.size} pages: ${internal} links within the site, ${github} to the repository at main, ` +
    `${outside} outside (not fetched). ${problems.length} problem(s).`,
);
process.exit(problems.length ? 1 : 0);
