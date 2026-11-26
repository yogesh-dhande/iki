// HTML for the plugin browser's pages, built as strings: the pages are large (a plugin's
// whole YAML, a schema's whole tree), so they're rendered here once rather than as nested
// components.
import { bySource, linksIn, RELATION_LABEL, type Browser } from "./browser.ts";
import { describe, esc, locKey, locLabel, rich, schemaHref, type SNode } from "./schemas.ts";

const code = (v: unknown): string =>
  `<code>${esc(typeof v === "string" ? v : JSON.stringify(v))}</code>`;

/** The links into a part or plugin, as disclosures: "Extended by 3", "Used by 12". */
export function linksInHtml(b: Browser, key: string, open = false): string {
  const groups = linksIn(b, key);
  if (!groups.length) return "";
  return `<div class="links-in">${groups
    .map(({ relation, refs }) => {
      const src = bySource(refs);
      const items = src
        .map(
          (s) =>
            `<li><a href="${esc(s.href)}">${esc(s.label)}</a> <span class="via">${s.paths.map((p) => esc(p)).join(", ")}</span></li>`,
        )
        .join("");
      return `<details${open && src.length <= 8 ? " open" : ""}><summary>${RELATION_LABEL[relation]} ${src.length}</summary><ul>${items}</ul></details>`;
    })
    .join("")}</div>`;
}

/** How many link in, by relation, for a table cell. */
export function linksInCount(b: Browser, key: string): string {
  return linksIn(b, key)
    .map(
      ({ relation, refs }) => `${RELATION_LABEL[relation].toLowerCase()} ${bySource(refs).length}`,
    )
    .join(", ");
}

function summaryHtml(n: SNode): string {
  const s = n.summary;
  const bits: string[] = [];
  if (s.types.length) bits.push(`<span class="st">${s.types.map(esc).join(" | ")}</span>`);
  if (s.ref)
    bits.push(`<a class="sref" href="${esc(schemaHref(s.ref))}">→ ${esc(locLabel(s.ref))}</a>`);
  if (s.const !== undefined) bits.push(`= ${code(s.const)}`);
  if (s.enum) bits.push(`one of ${s.enum.map((e) => code(e)).join(" ")}`);
  if (s.default !== undefined) bits.push(`default ${code(s.default)}`);
  for (const f of s.facts) bits.push(`<span class="sf">${esc(f)}</span>`);
  return bits.join(" · ");
}

/** A schema node, its keys and branches, each with an anchor the YAML's keys link to. */
export function schemaNodeHtml(b: Browser, n: SNode, depth = 0): string {
  const uses = b.uses.get(locKey(n.loc));
  const label =
    n.role === "prop" || n.role === "def"
      ? `<code class="sname">${esc(n.label)}</code>`
      : n.role === "branch"
        ? `<span class="sbranch">${rich(n.label)}</span>`
        : `<code class="sname smeta">${esc(n.label)}</code>`;
  const head = `<div class="shead"><a class="sanchor" href="#${esc(encodeURIComponent(n.id))}" aria-label="Link to ${esc(n.label)}">#</a>${label}${n.required ? ` <span class="req">required</span>` : ""} <span class="ssum">${summaryHtml(n)}</span></div>`;
  // A key whose schema is a shared definition shows the definition's description.
  const shared = !n.description && n.summary.ref ? describe(n.summary.ref) : undefined;
  const desc = n.description
    ? `<p class="sdesc">${rich(n.description)}</p>`
    : shared
      ? `<p class="sdesc shared">${rich(shared)}</p>`
      : "";
  const used = uses
    ? `<p class="suses">Used ${uses.count} time${uses.count === 1 ? "" : "s"}${uses.examples.length ? ": " : ""}${uses.examples
        .map(
          (e) =>
            `<a href="${esc(e.href)}" title="${esc(e.path)}">${esc(e.plugin.name)} ${esc(e.label)}</a>`,
        )
        .join(", ")}${uses.count > uses.examples.length ? ", …" : ""}</p>`
    : "";
  const kids = n.children.length
    ? `<div class="skids">${n.children.map((c) => schemaNodeHtml(b, c, depth + 1)).join("")}</div>`
    : "";
  return `<div class="snode s-${n.role}" id="${esc(n.id)}">${head}${desc}${used}${kids}</div>`;
}
