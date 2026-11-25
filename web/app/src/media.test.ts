import { describe, expect, it } from "vitest";

import { MediaFormats, type FileFormat } from "./media";
import { CORE_FORMATS } from "./test-server";

const media = new MediaFormats(CORE_FORMATS);

describe("media", () => {
  it("names lab formats the browser doesn't know, by extension", () => {
    expect(media.of({ name: "1ABC.PDB", type: "" })).toBe("chemical/x-pdb");
    // Some systems take .pdb for a Palm database.
    expect(media.of({ name: "1abc.pdb", type: "application/vnd.palm" })).toBe("chemical/x-pdb");
    expect(media.of({ name: "7xyz.cif", type: "" })).toBe("chemical/x-mmcif");
    expect(media.of({ name: "plate.fcs", type: "" })).toBe("application/vnd.isac.fcs");
    // SnapGene's own files, which systems without SnapGene don't know.
    expect(media.of({ name: "pUC19.dna", type: "" })).toBe("application/x-snapgene");
    expect(media.accept(["application/x-snapgene"])).toBe("application/x-snapgene,.dna");
  });

  it("keeps the browser's type for other files, or says it's unknown", () => {
    expect(media.of({ name: "gel.png", type: "image/png" })).toBe("image/png");
    expect(media.of({ name: "notes", type: "" })).toBe("application/octet-stream");
    expect(media.of({ name: ".pdb", type: "" })).toBe("application/octet-stream");
  });

  it("reads .fasta and .fa as the FASTA its field takes: they're used for both", () => {
    // UniProt's downloads are .fasta, and so are NCBI's nucleotide ones.
    const uniprot = { name: "P01308.fasta", type: "" };
    expect(media.of(uniprot, ["chemical/seq-aa-fasta", "text/plain"])).toBe(
      "chemical/seq-aa-fasta",
    );
    expect(media.of({ name: "L09137.fa", type: "" }, ["chemical/seq-na-fasta"])).toBe(
      "chemical/seq-na-fasta",
    );
    // A field that takes either, or anything, gets the nucleotide reading, as before.
    expect(media.of(uniprot)).toBe("chemical/seq-na-fasta");
    // .faa and .fna say which they are.
    expect(media.of({ name: "P01308.fna", type: "" }, ["chemical/seq-aa-fasta"])).toBe(
      "chemical/seq-na-fasta",
    );
    expect(media.of({ name: "P01308.faa", type: "" }, ["chemical/seq-na-fasta"])).toBe(
      "chemical/seq-aa-fasta",
    );
  });

  it("offers .fasta and .fa files to protein FASTA pickers", () => {
    expect(media.accept(["chemical/seq-aa-fasta", "text/plain"])).toBe(
      "chemical/seq-aa-fasta,text/plain,.fasta,.fa,.faa,.txt,.log,.aln,.a3m,.sto",
    );
    expect(media.accept(["chemical/seq-na-fasta"])).toBe("chemical/seq-na-fasta,.fasta,.fa,.fna");
  });

  it("names types the way scientists do, with their extensions", () => {
    expect(media.either(["chemical/seq-aa-fasta", "text/plain"])).toBe(
      "protein FASTA (.fasta, .fa or .faa) or plain-text (.txt, .log, .aln, .a3m or .sto)",
    );
    expect(media.either(["chemical/x-pdb", "chemical/x-mmcif", "image/*"])).toBe(
      "PDB (.pdb or .ent), mmCIF (.cif or .mmcif) or image",
    );
    expect(media.named("chemical/seq-na-genbank")).toBe("GenBank");
    expect(media.named("application/pdf")).toBe("PDF");
    // One it doesn't know is given as it is.
    expect(media.named("application/x-acme-plate")).toBe("application/x-acme-plate");
  });

  it("says why the file is refused before uploading it", async () => {
    await expect(
      media.identify(new File([], "notes.pdf"), ["iki/core.protein-fasta"]),
    ).rejects.toThrow(
      "notes.pdf is in PDF format. Choose a file matching protein FASTA (.fasta, .fa or .faa).",
    );
    await expect(media.identify(new File([], "plate"), ["chemical/x-pdb"])).rejects.toThrow(
      "plate isn’t a file this takes. Choose a file matching PDB (.pdb or .ent).",
    );
    await expect(media.identify(new File([], "1abc.pdb"), ["chemical/x-pdb"])).resolves.toBe(
      "chemical/x-pdb",
    );
  });

  it("uses every declared extension and media type from the generated core registry", () => {
    for (const format of CORE_FORMATS) {
      for (const extension of format.extensions) {
        expect(media.of({ name: `example${extension}`, type: "" }, [format.id])).toBe(
          format.canonical,
        );
      }
      for (const type of format.media_types) {
        expect(media.named(type)).toBe(format.label);
        expect(media.allowed(format.canonical, [type])).toBe(true);
      }
    }
  });

  it("recognizes a plugin's longest compound suffix, aliases and byte signatures", async () => {
    const format: FileFormat = {
      id: "lab/example.instrument",
      label: "Instrument data",
      canonical: "application/vnd.example.instrument",
      media_types: ["application/vnd.example.instrument", "application/x-example"],
      extensions: [".log.gz"],
      signatures: [{ offset: 1, hex: "313233" }],
      viewer: null,
      reader: null,
      parsers: [],
    };
    const installed = new MediaFormats([...CORE_FORMATS, format]);
    expect(installed.of({ name: "run.LOG.GZ", type: "" })).toBe(format.canonical);
    expect(installed.named("application/x-example")).toBe(format.label);
    expect(
      installed.choices([format.id, ...format.media_types, "image/*", "text/x-unknown"]),
    ).toEqual([format.id, "image/*", "text/x-unknown"]);
    expect(installed.either(format.media_types)).toBe("Instrument data (.log.gz)");
    // Bytes win over the browser and the suffix.
    await expect(
      installed.identify(new File(["!123ab"], "misnamed.txt"), [format.id]),
    ).resolves.toBe(format.canonical);
    await expect(installed.identify(new File(["other"], "run.log.gz"), [])).rejects.toThrow(
      "lacks the byte signature",
    );
    const other = {
      ...format,
      id: "lab/example.other",
      label: "Other",
      canonical: "text/x-other",
      media_types: ["text/x-other"],
      extensions: [".other"],
      signatures: [{ offset: 0, hex: "213132" }],
    };
    const ambiguous = new MediaFormats([format, other]);
    await expect(ambiguous.identify(new File(["!123"], "untitled"), [])).rejects.toThrow(
      "matches Instrument data and Other",
    );
    const longer = new MediaFormats([
      format,
      { ...other, signatures: [{ offset: 0, hex: "21313233" }] },
    ]);
    await expect(longer.identify(new File(["!123"], "untitled"), [])).resolves.toBe("text/x-other");
  });

  it("lets pickers offer files by extension too", () => {
    expect(media.accept([])).toBeUndefined();
    expect(media.accept(["chemical/x-mmcif", "chemical/x-pdb"])).toBe(
      "chemical/x-mmcif,chemical/x-pdb,.pdb,.ent,.cif,.mmcif",
    );
    expect(media.accept(["image/*"])).toBe("image/*,.tif,.tiff,.ome.tif,.ome.tiff,.jpg,.jpeg,.png");
  });

  it("allows wildcards", () => {
    expect(media.allowed("image/png", ["image/*"])).toBe(true);
    expect(media.allowed("text/plain", ["image/*"])).toBe(false);
    expect(media.allowed("anything/at-all", [])).toBe(true);
  });
});
