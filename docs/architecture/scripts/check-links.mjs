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

for (const p of problems) console.error(p);
console.log(
  `${html.size} pages: ${internal} links within the site, ${github} to the repository at main, ` +
    `${outside} outside (not fetched). ${problems.length} problem(s).`,
);
process.exit(problems.length ? 1 : 0);
