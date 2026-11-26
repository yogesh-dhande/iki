// How the site shows a difference's HGVS name: in full, unless it spells out a long
// stretch of bases, which a table shows by its first ten and its length.
export function shortHgvs(hgvs: string, length = 10): { text: string; bases: number | null } {
  const match = /^(.*?(?:ins|delins|dup))([ACGTN]+)$/.exec(hgvs);
  const head = match?.[1];
  const bases = match?.[2];
  if (!head || !bases || bases.length <= length) {
    return { text: hgvs, bases: null };
  }
  return { text: `${head}${bases.slice(0, length)}…`, bases: bases.length };
}

const KINDS: Record<string, string> = {
  substitution: "substitution",
  deletion: "deletion",
  insertion: "insertion",
  deletion_insertion: "deletion-insertion",
  duplication: "duplication",
  inversion: "inversion",
};

export function kindOf(kind: string, lengthChange: number): string {
  const name = KINDS[kind] ?? kind;
  return kind === "insertion" && lengthChange > 1 ? `${name}, ${String(lengthChange)} bp` : name;
}

/** "lac promoter (promoter)" → "lac promoter": the feature's name, without its type. */
export function featureName(feature: string): string {
  return feature.replace(/\s*\([^)]*\)$/, "");
}
