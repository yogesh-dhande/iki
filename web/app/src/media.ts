/**
 * Media types, as file fields' rules name them: `image/*` allows any image.
 *
 * Browsers know a file's type only from what the operating system maps its
 * extension to, and most systems don't know scientific formats: a `.pdb` file
 * arrives with no type (or as a Palm database). The table below names the
 * types of common lab formats by extension, as shared-mime-info and IANA do,
 * and wins over the browser's guess for those extensions.
 *
 * Some extensions are used for more than one type: `.fasta` and `.fa` hold
 * protein sequences as often as nucleotide ones (UniProt's downloads are
 * `.fasta`). Such a file is the type its field takes.
 */

const NA_FASTA = "chemical/seq-na-fasta";
const AA_FASTA = "chemical/seq-aa-fasta";

/** By extension, the types its files may be, the usual one first. */
const BY_EXTENSION: Readonly<Record<string, readonly string[]>> = {
  // Structures and molecules
  pdb: ["chemical/x-pdb"],
  ent: ["chemical/x-pdb"],
  cif: ["chemical/x-mmcif"],
  mmcif: ["chemical/x-mmcif"],
  mol: ["chemical/x-mdl-molfile"],
  sdf: ["chemical/x-mdl-sdfile"],
  mol2: ["chemical/x-mol2"],
  xyz: ["chemical/x-xyz"],
  cml: ["chemical/x-cml"],
  smi: ["chemical/x-daylight-smiles"],
  // Sequences
  fasta: [NA_FASTA, AA_FASTA],
  fa: [NA_FASTA, AA_FASTA],
  fna: [NA_FASTA],
  faa: [AA_FASTA],
  gb: ["chemical/seq-na-genbank"],
  gbk: ["chemical/seq-na-genbank"],
  fastq: ["text/x-fastq"],
  fq: ["text/x-fastq"],
  sam: ["text/x-sam"],
  bam: ["application/x-bam"],
  ab1: ["application/x-abi"],
  // Instruments and data
  fcs: ["application/vnd.isac.fcs"],
  mzml: ["application/x-mzml"],
  h5: ["application/x-hdf5"],
  hdf5: ["application/x-hdf5"],
  csv: ["text/csv"],
  tsv: ["text/tab-separated-values"],
  json: ["application/json"],
  xlsx: ["application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"],
  // Images that systems often miss
  tif: ["image/tiff"],
  tiff: ["image/tiff"],
  czi: ["application/x-czi"],
};

/** What people call a type, and whether it's "an" one. */
const NAMES: Readonly<Record<string, { name: string; an?: true }>> = {
  "chemical/x-pdb": { name: "PDB" },
  "chemical/x-mmcif": { name: "mmCIF", an: true },
  "chemical/x-mdl-molfile": { name: "MOL" },
  "chemical/x-mdl-sdfile": { name: "SDF", an: true },
  "chemical/x-mol2": { name: "MOL2" },
  "chemical/x-xyz": { name: "XYZ", an: true },
  "chemical/x-cml": { name: "CML" },
  "chemical/x-daylight-smiles": { name: "SMILES" },
  [NA_FASTA]: { name: "nucleotide FASTA" },
  [AA_FASTA]: { name: "protein FASTA" },
  "chemical/seq-na-genbank": { name: "GenBank" },
  "text/x-fastq": { name: "FASTQ" },
  "text/x-sam": { name: "SAM", an: true },
  "application/x-bam": { name: "BAM" },
  "application/x-abi": { name: "ABI trace", an: true },
  "application/vnd.isac.fcs": { name: "FCS", an: true },
  "application/x-mzml": { name: "mzML", an: true },
  "application/x-hdf5": { name: "HDF5", an: true },
  "text/csv": { name: "CSV" },
  "text/tab-separated-values": { name: "TSV" },
  "application/json": { name: "JSON" },
  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": {
    name: "Excel",
    an: true,
  },
  "application/pdf": { name: "PDF" },
  "text/plain": { name: "plain-text" },
  "image/*": { name: "image", an: true },
  "image/png": { name: "PNG" },
  "image/jpeg": { name: "JPEG" },
  "image/tiff": { name: "TIFF" },
  "application/x-czi": { name: "Zeiss CZI" },
};

/** "a", "a or b", "a, b or c". */
function or(items: readonly string[]): string {
  return items.length > 1
    ? `${items.slice(0, -1).join(", ")} or ${items.at(-1) ?? ""}`
    : (items[0] ?? "");
}

function extensionOf(name: string): string {
  const dot = name.lastIndexOf(".");
  return dot > 0 ? name.slice(dot + 1).toLowerCase() : "";
}

/** The extensions the table gives files of `type`, as ".pdb or .ent". */
function extensionsOf(type: string): string {
  const found = Object.entries(BY_EXTENSION)
    .filter(([, types]) => types.includes(type))
    .map(([extension]) => `.${extension}`);
  return or(found);
}

export const media = {
  allowed(mediaType: string, allowed: readonly string[]): boolean {
    if (allowed.length === 0) return true;
    const kind = mediaType.split("/", 1)[0] ?? "";
    return allowed.some((a) => a === mediaType || a === `${kind}/*`);
  },

  /** "protein FASTA", or the type itself for one it doesn't know. */
  named(mediaType: string): string {
    return NAMES[mediaType]?.name ?? mediaType;
  },

  /** "PDB (.pdb or .ent) or mmCIF (.cif or .mmcif)": what people would call `choices`. */
  either(choices: readonly string[]): string {
    return or(
      choices.map((type) => {
        const extensions = extensionsOf(type);
        return extensions ? `${media.named(type)} (${extensions})` : media.named(type);
      }),
    );
  },

  /**
   * A file's media type: the table's for extensions it knows, else the browser's. An
   * extension used for several, such as `.fasta`, is the one of them `allowed` takes.
   */
  of(file: { name: string; type: string }, allowed: readonly string[] = []): string {
    const types = BY_EXTENSION[extensionOf(file.name)];
    if (!types) return file.type || "application/octet-stream";
    const usual = types[0] ?? "application/octet-stream";
    if (allowed.length === 0) return usual;
    return types.find((t) => media.allowed(t, allowed)) ?? usual;
  },

  /** Why `file` isn't one of `allowed`, for the person who chose it, or null if it is. */
  refusal(file: { name: string; type: string }, allowed: readonly string[]): string | null {
    const type = media.of(file, allowed);
    if (media.allowed(type, allowed)) return null;
    const known = NAMES[type];
    const first = NAMES[allowed[0] ?? ""];
    return (
      (known
        ? `${file.name} is ${known.an ? "an" : "a"} ${known.name} file.`
        : `${file.name} isn’t a file this takes.`) +
      ` Choose ${first?.an ? "an" : "a"} ${media.either(allowed)} file.`
    );
  },

  /**
   * The file picker's `accept`: the types, and the extensions the table maps to
   * them, so pickers that don't know a type still offer its files.
   */
  accept(allowed: readonly string[]): string | undefined {
    if (allowed.length === 0) return undefined;
    const extensions = Object.entries(BY_EXTENSION)
      .filter(([, types]) => types.some((type) => media.allowed(type, allowed)))
      .map(([extension]) => `.${extension}`);
    return [...allowed, ...extensions].join(",");
  },
};
