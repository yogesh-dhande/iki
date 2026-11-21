import { describe, expect, it } from "vitest";

import { media } from "./media";

describe("media", () => {
  it("names lab formats the browser doesn't know, by extension", () => {
    expect(media.of({ name: "1ABC.PDB", type: "" })).toBe("chemical/x-pdb");
    // Some systems take .pdb for a Palm database.
    expect(media.of({ name: "1abc.pdb", type: "application/vnd.palm" })).toBe("chemical/x-pdb");
    expect(media.of({ name: "7xyz.cif", type: "" })).toBe("chemical/x-mmcif");
    expect(media.of({ name: "plate.fcs", type: "" })).toBe("application/vnd.isac.fcs");
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
      "chemical/seq-aa-fasta,text/plain,.fasta,.fa,.faa",
    );
    expect(media.accept(["chemical/seq-na-fasta"])).toBe("chemical/seq-na-fasta,.fasta,.fa,.fna");
  });

  it("names types the way scientists do, with their extensions", () => {
    expect(media.either(["chemical/seq-aa-fasta", "text/plain"])).toBe(
      "protein FASTA (.fasta, .fa or .faa) or plain-text",
    );
    expect(media.either(["chemical/x-pdb", "chemical/x-mmcif", "image/*"])).toBe(
      "PDB (.pdb or .ent), mmCIF (.cif or .mmcif) or image",
    );
    expect(media.named("chemical/seq-na-genbank")).toBe("GenBank");
    expect(media.named("application/pdf")).toBe("PDF");
    // One it doesn't know is given as it is.
    expect(media.named("application/x-acme-plate")).toBe("application/x-acme-plate");
  });

  it("says why a file isn't one a field takes", () => {
    expect(
      media.refusal({ name: "notes.pdf", type: "application/pdf" }, ["chemical/seq-aa-fasta"]),
    ).toBe("notes.pdf is a PDF file. Choose a protein FASTA (.fasta, .fa or .faa) file.");
    expect(media.refusal({ name: "plate", type: "" }, ["chemical/x-pdb", "text/plain"])).toBe(
      "plate isn’t a file this takes. Choose a PDB (.pdb or .ent) or plain-text file.",
    );
    expect(media.refusal({ name: "1abc.pdb", type: "" }, ["chemical/x-pdb"])).toBeNull();
  });

  it("lets pickers offer files by extension too", () => {
    expect(media.accept([])).toBeUndefined();
    expect(media.accept(["chemical/x-mmcif", "chemical/x-pdb"])).toBe(
      "chemical/x-mmcif,chemical/x-pdb,.pdb,.ent,.cif,.mmcif",
    );
    expect(media.accept(["image/*"])).toBe("image/*,.tif,.tiff");
  });

  it("allows wildcards", () => {
    expect(media.allowed("image/png", ["image/*"])).toBe(true);
    expect(media.allowed("text/plain", ["image/*"])).toBe(false);
    expect(media.allowed("anything/at-all", [])).toBe(true);
  });
});
