// The repository this site describes, read at build time. Every path a page cites goes
// through here, so a file that moves or a symbol that's renamed fails the build instead
// of leaving a dead link.
import { execFileSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import path from "node:path";

/** The repository root: the site is built from docs/architecture. */
export const REPO = path.resolve(process.cwd(), "../..");
if (!existsSync(path.join(REPO, "docs/adr"))) {
  throw new Error(`Build the architecture site from docs/architecture (no docs/adr in ${REPO}).`);
}

export const GITHUB = "https://github.com/yogesh-dhande/iki";

let tracked: Set<string> | undefined;
let trackedDirs: Set<string> | undefined;

function load(): void {
  const out = execFileSync(
    "git",
    ["ls-files", "-z", "--cached", "--others", "--exclude-standard"],
    {
      cwd: REPO,
      encoding: "utf8",
      maxBuffer: 256 * 1024 * 1024,
    },
  );
  tracked = new Set(out.split("\0").filter(Boolean));
  trackedDirs = new Set();
  for (const file of tracked) {
    const parts = file.split("/");
    for (let i = 1; i < parts.length; i++) trackedDirs.add(parts.slice(0, i).join("/"));
  }
}

function sets(): { files: Set<string>; dirs: Set<string> } {
  if (!tracked || !trackedDirs) load();
  if (!tracked || !trackedDirs) throw new Error("git ls-files gave nothing.");
  return { files: tracked, dirs: trackedDirs };
}

export interface RepoPath {
  path: string;
  kind: "file" | "dir";
}

/** A repo-relative path, checked to be a file or directory git knows about. */
export function resolvePath(p: string): RepoPath {
  const clean = p.replace(/\/+$/, "");
  for (const fresh of [false, true]) {
    // A long-running dev server lists files once; a file added since is listed on a miss.
    if (fresh) load();
    const { files, dirs } = sets();
    if (files.has(clean)) return { path: clean, kind: "file" };
    if (dirs.has(clean)) return { path: clean, kind: "dir" };
  }
  throw new Error(`The architecture site cites ${p}, which isn't in the repository.`);
}

/** GitHub's page for a path at main: a blob for a file, a tree for a directory. */
export function githubUrl(p: string): string {
  const { path: clean, kind } = resolvePath(p);
  return `${GITHUB}/${kind === "file" ? "blob" : "tree"}/main/${clean}`;
}

/** Checks that a file still says what a page quotes from it, such as a function's name. */
export function assertContains(p: string, needle: string): void {
  const { path: clean, kind } = resolvePath(p);
  if (kind !== "file") throw new Error(`${p} is a directory; a symbol needs a file.`);
  const text = readFileSync(path.join(REPO, clean), "utf8");
  if (!text.includes(needle)) {
    throw new Error(`The architecture site says ${clean} contains "${needle}", and it doesn't.`);
  }
}

export function readRepo(p: string): string {
  return readFileSync(path.join(REPO, resolvePath(p).path), "utf8");
}

/** Tracked files under a directory, repo-relative. */
export function filesUnder(dir: string): string[] {
  const prefix = dir.replace(/\/+$/, "") + "/";
  return [...sets().files].filter((f) => f.startsWith(prefix)).sort();
}

/** The commit the site was built from, for the footer. */
export function headCommit(): string {
  return execFileSync("git", ["rev-parse", "--short", "HEAD"], {
    cwd: REPO,
    encoding: "utf8",
  }).trim();
}
