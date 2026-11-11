/** Media types, as file fields' rules name them: `image/*` allows any image. */

export const media = {
  allowed(mediaType: string, allowed: readonly string[]): boolean {
    if (allowed.length === 0) return true;
    const kind = mediaType.split("/", 1)[0] ?? "";
    return allowed.some((a) => a === mediaType || a === `${kind}/*`);
  },

  /** "chemical/x-pdb or chemical/x-mmcif", as the server says it. */
  either(choices: readonly string[]): string {
    if (choices.length <= 1) return choices[0] ?? "";
    return `${choices.slice(0, -1).join(", ")} or ${choices.at(-1) ?? ""}`;
  },
};
