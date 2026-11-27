// The module map, read from the repository at build time: the uv workspace's Python
// packages and the server's Django apps, the worker's Rust crates, and the pnpm workspace's
// web packages, each with the purpose it states for itself and the site's sections that
// cite files inside it.
import { existsSync, readFileSync } from "node:fs";
import path from "node:path";
import { NAV } from "./nav.ts";
import { filesUnder, readRepo, REPO, resolvePath } from "./repo.ts";

export interface Module {
  name: string;
  dir: string;
  purpose: string;
  /** Where the purpose was read from. */
  from: string;
  covered: { label: string; href: string }[];
}

/** The first sentence of a text, joined onto one line. */
function sentence(text: string): string {
  const flat = text.replace(/\s+/g, " ").trim();
  const end = flat.search(/\.(\s|$)/);
  return end < 0 ? flat : flat.slice(0, end + 1);
}

function docstring(file: string): string | undefined {
  const m = /^\s*(?:#.*\n)*\s*"""([\s\S]*?)"""/.exec(readRepo(file));
  // Sphinx markup, such as ``/api/v1`` and :mod:`.backend`, read as plain text.
  const text = m?.[1]?.replace(/``([^`]+)``/g, "$1").replace(/:\w+:`([^`]+)`/g, "$1");
  return text ? sentence(text) : undefined;
}

function tomlField(file: string, key: string): string {
  const m = new RegExp(`^${key} = "([^"]*)"`, "m").exec(readRepo(file));
  if (!m) throw new Error(`${file} has no ${key}.`);
  return m[1];
}

// Each section's page source, so a module is "covered" by the pages whose source names a
// path inside it.
const sources = NAV.flatMap((g) => g.items)
  .filter((i) => i.href !== "/modules/")
  .map((item) => {
    const slug = item.href.slice(1, -1);
    const base = "docs/architecture/src/pages";
    const page = slug === "" ? "index.astro" : `${slug}.astro`;
    const file = existsSync(path.join(REPO, base, page)) ? page : `${slug}/index.astro`;
    const text = readFileSync(path.join(REPO, base, file), "utf8");
    return { item, text };
  });

function coveredBy(dir: string): Module["covered"] {
  const pattern = new RegExp(
    `(?<=["'\`\\s(]|^)${dir.replace(/[.*+?^${}()|[\]\\/]/g, "\\$&")}(?=[/"'\`]|$)`,
    "m",
  );
  return sources.filter((s) => pattern.test(s.text)).map((s) => s.item);
}

function make(name: string, dir: string, purpose: string, from: string): Module {
  resolvePath(dir);
  return { name, dir, purpose, from, covered: coveredBy(dir) };
}

/** The uv workspace's packages, from the root pyproject.toml's members. */
export function pythonPackages(): Module[] {
  const root = readRepo("pyproject.toml");
  const m = /^members = \[([^\]]*)\]/m.exec(root);
  if (!m) throw new Error("pyproject.toml has no workspace members.");
  return [...m[1].matchAll(/"([^"]+)"/g)].map(([, member]) => {
    const toml = `${member}/pyproject.toml`;
    return make(tomlField(toml, "name"), member, tomlField(toml, "description"), toml);
  });
}

/** The server's Django apps: each package under iki_server with an apps.py. */
export function djangoApps(): Module[] {
  const base = "server/src/iki_server";
  const apps = new Set(
    filesUnder(base)
      .filter((f) => /^server\/src\/iki_server\/[^/]+\/apps\.py$/.test(f))
      .map((f) => f.split("/")[3]),
  );
  return [...apps].sort().map((app) => {
    const dir = `${base}/${app}`;
    for (const file of [`${dir}/__init__.py`, `${dir}/models.py`]) {
      try {
        const doc = docstring(file);
        if (doc) return make(app, dir, doc, file);
      } catch {
        // No such file: try the next.
      }
    }
    const verbose = /verbose_name = "([^"]+)"/.exec(readRepo(`${dir}/apps.py`))?.[1] ?? app;
    return make(app, dir, verbose, `${dir}/apps.py`);
  });
}

/** The worker's crates. */
export function rustCrates(): Module[] {
  const crates = new Set(
    filesUnder("worker/crates")
      .filter((f) => /^worker\/crates\/[^/]+\/Cargo\.toml$/.test(f))
      .map((f) => f.split("/")[2]),
  );
  return [...crates].sort().map((c) => {
    const toml = `worker/crates/${c}/Cargo.toml`;
    return make(
      tomlField(toml, "name"),
      `worker/crates/${c}`,
      tomlField(toml, "description"),
      toml,
    );
  });
}

/** The web app and the packages beside it. */
export function webPackages(): Module[] {
  const dirs = [
    "web/app",
    ...new Set(
      filesUnder("web/packages")
        .filter((f) => /^web\/packages\/[^/]+\/package\.json$/.test(f))
        .map((f) => f.split("/").slice(0, 3).join("/")),
    ),
  ];
  return dirs.map((dir) => {
    const pkg = JSON.parse(readRepo(`${dir}/package.json`)) as {
      name?: string;
      description?: string;
    };
    return make(pkg.name ?? dir, dir, pkg.description ?? "", `${dir}/package.json`);
  });
}
