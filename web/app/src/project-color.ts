/**
 * A project's color, and a person's (ADR 0176): a small square beside a project's name, a
 * circle behind a person's initials. Projects have no color of their own to choose, so each
 * gets one of eight from its ID, the same one wherever it's shown and for everyone. The
 * swatches are `--swatch-1` to `--swatch-8` in iki.css: at least 3:1 against the page in
 * both themes, and `--swatch-text` on each at 4.5:1, for initials.
 */

export const SWATCHES = 8;

/**
 * Which swatch an ID or a type's name takes, from 1 to {@link SWATCHES}: FNV-1a over its
 * characters, then MurmurHash3's finalizer. FNV-1a's low bits come only from its
 * characters' low bits, so without the mixing, names that share a prefix (`iki/lots.`)
 * crowd onto a few swatches: of the dev data's 24 types, 8 took one.
 */
export function swatchOf(id: string): number {
  let hash = 0x811c9dc5;
  for (let i = 0; i < id.length; i += 1) {
    hash ^= id.charCodeAt(i);
    hash = Math.imul(hash, 0x01000193);
  }
  hash ^= hash >>> 16;
  hash = Math.imul(hash, 0x85ebca6b);
  hash ^= hash >>> 13;
  hash = Math.imul(hash, 0xc2b2ae35);
  hash ^= hash >>> 16;
  return ((hash >>> 0) % SWATCHES) + 1;
}

/** The CSS color an ID's swatch is, for a `style`'s `background`. */
export function swatchColor(id: string): string {
  return `var(--swatch-${String(swatchOf(id))})`;
}

/** Up to two initials of a name, as "Ada Lovelace" is "AL"; an email's first letter. */
export function initials(name: string): string {
  const words = name
    .replace(/@.*$/, "")
    .split(/[\s._-]+/)
    .filter(Boolean);
  const first = words[0]?.[0] ?? "?";
  const last = words.length > 1 ? (words[words.length - 1]?.[0] ?? "") : "";
  return (first + last).toUpperCase();
}
