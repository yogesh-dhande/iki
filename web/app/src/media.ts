/**
 * Media types, as file fields' rules name them: `image/*` allows any image.
 *
 * Browsers know a file's type only from what the operating system maps its
 * extension to, and most systems don't know scientific formats: a `.pdb` file
 * arrives with no type (or as a Palm database). The table below names the
 * types of common lab formats by extension, as shared-mime-info and IANA do,
 * and wins over the browser's guess for those extensions.
 */

const BY_EXTENSION: Readonly<Record<string, string>> = {
  // Structures and molecules
  pdb: "chemical/x-pdb",
  ent: "chemical/x-pdb",
  cif: "chemical/x-mmcif",
  mmcif: "chemical/x-mmcif",
  mol: "chemical/x-mdl-molfile",
  sdf: "chemical/x-mdl-sdfile",
  mol2: "chemical/x-mol2",
  xyz: "chemical/x-xyz",
  cml: "chemical/x-cml",
  smi: "chemical/x-daylight-smiles",
  // Sequences
  fasta: "chemical/seq-na-fasta",
  fa: "chemical/seq-na-fasta",
  fna: "chemical/seq-na-fasta",
  faa: "chemical/seq-aa-fasta",
  gb: "chemical/seq-na-genbank",
  gbk: "chemical/seq-na-genbank",
  fastq: "text/x-fastq",
  fq: "text/x-fastq",
  sam: "text/x-sam",
  bam: "application/x-bam",
  ab1: "application/x-abi",
  // Instruments and data
  fcs: "application/vnd.isac.fcs",
  mzml: "application/x-mzml",
  h5: "application/x-hdf5",
  hdf5: "application/x-hdf5",
  csv: "text/csv",
  tsv: "text/tab-separated-values",
  json: "application/json",
  xlsx: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  // Images that systems often miss
  tif: "image/tiff",
  tiff: "image/tiff",
  czi: "application/x-czi",
};

function extensionOf(name: string): string {
  const dot = name.lastIndexOf(".");
  return dot > 0 ? name.slice(dot + 1).toLowerCase() : "";
}

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

  /** A file's media type: the table's for extensions it knows, else the browser's. */
  of(file: { name: string; type: string }): string {
    return BY_EXTENSION[extensionOf(file.name)] ?? (file.type || "application/octet-stream");
  },

  /**
   * The file picker's `accept`: the types, and the extensions the table maps to
   * them, so pickers that don't know a type still offer its files.
   */
  accept(allowed: readonly string[]): string | undefined {
    if (allowed.length === 0) return undefined;
    const extensions = Object.entries(BY_EXTENSION)
      .filter(([, type]) => media.allowed(type, allowed))
      .map(([extension]) => `.${extension}`);
    return [...allowed, ...extensions].join(",");
  },
};
