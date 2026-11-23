// @vitest-environment node
import { readFile } from "node:fs/promises";
import { describe, expect, it, vi } from "vitest";

import { describeHelm, isProblem, loadParsers, MAX_READ_BYTES } from "./parsers.js";

const binary = new URL("../wasm/iki_wasm.wasm", import.meta.url);

describe("the packaged WebAssembly parsers", () => {
  it("reads alignment coordinates and returns line numbers for malformed input", async () => {
    const compiled = await WebAssembly.compile(await readFile(binary));
    const { readFasta } = await loadParsers(compiled);
    // Synthetic aligned nucleotide fragments with a reversed range on the second row.
    const file = new TextEncoder().encode(">first\nAC-T\n>second/8-6 label\nAC.T\n");
    expect(readFasta(file, "nucleotide")).toMatchObject({
      kind: "alignment",
      length: 4,
      alphabet_guessed: false,
      consensus: "AC-T",
      rows: [
        { id: "first", aligned: "AC-T", residues: 3 },
        { id: "second", start: 8, reverse: true, description: "label" },
      ],
    });
    expect(readFasta(">broken\nAT1")).toMatchObject({ kind: "error", line: 2 });
    expect(() => readFasta(new Uint8Array(MAX_READ_BYTES + 1))).toThrow(RangeError);
    expect(readFasta(">short\nA\n>long\nAA")).toMatchObject({
      kind: "sequences",
      records: [{ length: 1 }, { length: 2 }],
    });
  });

  it("returns independent HELM readings from the same module", async () => {
    const { readHelm } = await loadParsers(await WebAssembly.compile(await readFile(binary)));
    // A two-monomer peptide in the HELM 2.04 grammar (Pistoia HELMNotationStandard).
    const [valid, invalid] = readHelm(["PEPTIDE1{G.A}$$$$V2.0", "PEPTIDE1{G.}$$$$V2.0"]);
    if (!valid || !invalid) throw new Error("Expected both HELM readings.");
    expect(isProblem(valid)).toBe(false);
    expect(describeHelm(valid)).toBe("HELM: a chain of 2 amino acids.");
    expect(invalid).toMatchObject({ at: 11 });
    expect(isProblem(invalid) && typeof invalid.error).toBe("string");
    expect(readHelm([])).toEqual([]);
  });

  it("lets the host load the binary from a URL and retry a failed fetch", async () => {
    const bytes = await readFile(binary);
    const fetchBinary = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(new Response(null, { status: 503 }))
      .mockResolvedValueOnce(new Response(bytes));
    vi.stubGlobal("fetch", fetchBinary);
    try {
      const url = new URL("https://example.test/assets/parser.wasm");
      await expect(loadParsers(url)).rejects.toThrow("503");
      const { readFasta } = await loadParsers(url);
      expect(readFasta("invalid")).toMatchObject({ kind: "error", line: 1 });
      expect(fetchBinary).toHaveBeenCalledWith(url);
    } finally {
      vi.unstubAllGlobals();
    }
  });
});
