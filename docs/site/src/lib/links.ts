// Links in the repository's Markdown name files in the repository, as they do on GitHub:
// an ADR links to `0184-a-plasmid-prep-….md`, and to `../../plugins/lots/README.md`. When
// the site renders such a file, each link goes to the page that file is, if it is one,
// or else to the file on GitHub. So one Markdown file reads the same in both places, and
// the built site's check (python -m sitegen check) checks its links like any other's.
import { existsSync, statSync } from "node:fs";
import { posix } from "node:path";
import { fileURLToPath } from "node:url";

import { defineMdastPlugin, type MdastPluginEntry } from "satteri";

export interface Options {
  /** The repository's root, as a path. */
  root: string;
  /** Each repository file that is a page, and its path on the site. */
  sources: Record<string, string>;
  /** Where the repository's files are on GitHub, such as `https://github.com/<owner>/iki`. */
  repository: string;
  /** The branch or tag links to files point at. */
  ref: string;
}

const SCHEME = /^[a-z][a-z0-9+.-]*:/i;
const REPO = "repo:";

/** Where a link in `source` (a file, from the repository's root) goes on the site. */
export function rewrite(url: string, source: string, options: Options): string {
  if (url.startsWith(REPO)) {
    // A page written for the site naming a repository file: `repo:plugins/lots/README.md`.
    return rewrite(`/${url.slice(REPO.length)}`, source, options);
  }
  if (SCHEME.test(url) || url.startsWith("#") || url.startsWith("//") || url === "") {
    return url;
  }
  const match = /^([^?#]*)(.*)$/.exec(url);
  const path = match?.[1] ?? url;
  const rest = match?.[2] ?? "";
  const named = path.startsWith("/")
    ? posix.normalize(path.slice(1))
    : posix.normalize(posix.join(posix.dirname(source), path));
  if (named === ".." || named.startsWith("../")) {
    // Past the repository's root: one of GitHub's own pages, such as ../../issues/new.
    return new URL(url, `${options.repository}/blob/${options.ref}/${source}`).href;
  }
  const file = named.replace(/\/$/, "") || ".";
  const page = options.sources[file] ?? options.sources[posix.join(file, "README.md")];
  if (page !== undefined) {
    return page + rest;
  }
  const onDisk = posix.join(options.root, file);
  if (existsSync(onDisk)) {
    const kind = statSync(onDisk).isDirectory() ? "tree" : "blob";
    return `${options.repository}/${kind}/${options.ref}/${file === "." ? "" : file}${rest}`;
  }
  // Nothing there: left as written, so the built site's check reports it.
  return url;
}

/** The Markdown plugin (Sätteri's, Astro's processor): rewrites links in the repository's
 * own files. Pages written for the site link to the site's paths, which are left alone,
 * and name repository files as `repo:<path>`, which go where that file's links would. */
export function repositoryLinks(options: Options): MdastPluginEntry {
  return ({ fileURL }) => {
    if (fileURL === undefined) {
      return null;
    }
    const source = posix.relative(options.root, fileURLToPath(fileURL));
    if (source.startsWith("..")) {
      return null;
    }
    const own = source.startsWith("docs/site/");
    const to = (url: string) =>
      own && !url.startsWith(REPO) ? url : rewrite(url, source, options);
    return defineMdastPlugin({
      name: "iki-repository-links",
      link(node, context) {
        context.setField(node, "url", to(node.url));
      },
      definition(node, context) {
        context.setField(node, "url", to(node.url));
      },
    });
  };
}
