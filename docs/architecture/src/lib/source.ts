// Small readers of the repository's source, for facts the pages show as they are in the
// code: a block of a YAML manifest, a Django TextChoices enum, a directory's files.
import { readRepo } from "./repo.ts";

/**
 * The block of `file` that starts at the first line matching `start` (after any earlier
 * line matching `within`), up to the next line indented as little or less: the whole of
 * a YAML key with its children, comments included. Fails the build if it isn't there.
 */
export function block(file: string, start: RegExp, within?: RegExp, toEnd = false): string {
  const lines = readRepo(file).split("\n");
  let from = 0;
  if (within) {
    from = lines.findIndex((l) => within.test(l));
    if (from < 0) throw new Error(`${file} has no line matching /${within.source}/.`);
  }
  const at = lines.findIndex((l, i) => i >= from && start.test(l));
  if (at < 0) throw new Error(`${file} has no line matching /${start.source}/.`);
  const indent = (l: string): number => l.length - l.trimStart().length;
  const base = indent(lines[at]);
  let end = toEnd ? lines.length : at + 1;
  while (end < lines.length) {
    const l = lines[end];
    if (l.trim() && indent(l) <= base && !l.trimStart().startsWith("#")) break;
    end += 1;
  }
  while (end > at + 1 && !lines[end - 1].trim()) end -= 1;
  return lines.slice(at, end).join("\n");
}

export interface Choice {
  value: string;
  label: string;
  note: string;
}

/** A Django TextChoices class's members, in order, with their `#:` comments. */
export function textChoices(file: string, cls: string): Choice[] {
  const lines = readRepo(file).split("\n");
  const at = lines.findIndex((l) => new RegExp(`^class ${cls}\\(models\\.TextChoices\\):`).test(l));
  if (at < 0) throw new Error(`${file} no longer defines ${cls}(models.TextChoices).`);
  const out: Choice[] = [];
  let note: string[] = [];
  for (const line of lines.slice(at + 1)) {
    if (line.trim() && !line.startsWith(" ")) break;
    const comment = /^\s+#:\s?(.*)$/.exec(line);
    if (comment) {
      note.push(comment[1]);
      continue;
    }
    const m = /^\s+[A-Z_]+ = "([^"]+)", "([^"]+)"/.exec(line);
    if (m) {
      out.push({ value: m[1], label: m[2], note: note.join(" ") });
    }
    note = [];
  }
  if (!out.length) throw new Error(`${file}'s ${cls} has no members this reader understands.`);
  return out;
}

/** A Python module-level tuple or frozenset of strings, such as TIERS. */
export function stringTuple(file: string, name: string): string[] {
  const text = readRepo(file);
  const m = new RegExp(
    `^${name}(?::[^=]*)? = (?:frozenset\\()?[({\\[]([\\s\\S]*?)[)}\\]]`,
    "m",
  ).exec(text);
  if (!m) throw new Error(`${file} no longer defines ${name}.`);
  return [...m[1].matchAll(/"([^"]+)"/g)].map((x) => x[1]);
}
