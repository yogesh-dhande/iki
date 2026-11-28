// @vitest-environment node
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { describe, expect, it, vi } from "vitest";
import { meltingTemperature } from "./oligo-temperature.js";

interface ScalarModule {
  memory: WebAssembly.Memory;
  iki_input: (length: number) => number;
  iki_melting_temperature_value: () => number;
}

/** The oligos server/tests/data/record_melting_temperatures.py records, made the same way:
 * 20,000 drawn from SHA-256, then every palindrome of 4 to 10 bases. */
function oligos(): string[] {
  const bases = ["A", "C", "G", "T"];
  const made: string[] = [];
  for (let i = 0; i < 20_000; i++) {
    const digest = createHash("sha256")
      .update("iki-oligo-" + String(i))
      .digest();
    const length = 6 + (digest.readUInt8(0) % 55);
    let bits = BigInt("0x" + digest.subarray(1).toString("hex"));
    const drawn: string[] = [];
    for (let k = 0; k < length; k++, bits >>= 2n) drawn.push(bases[Number(bits & 3n)] ?? "");
    made.push(drawn.join(""));
  }
  for (let size = 2; size <= 5; size++) {
    for (let n = 0; n < 4 ** size; n++) {
      const left: number[] = [];
      for (let k = size - 1; k >= 0; k--) left.push(Math.floor(n / 4 ** k) % 4);
      // A base's complement is the one three places along A, C, G, T.
      const right = left.map((base) => 3 - base).reverse();
      made.push([...left, ...right].map((base) => bases[base] ?? "").join(""));
    }
  }
  return made;
}

describe("the supplied DNA strand’s melting temperature", () => {
  it("is the one 50 mM Na⁺ temperature every Iki module gives, for 21,360 oligos", async () => {
    // Recorded from the server's and both plugins' modules, which give the same number
    // for each oligo (ADR 0191); the browser's translation of the measurement gives it too.
    const recorded = JSON.parse(
      await readFile(
        new URL("../../../../server/tests/data/melting_temperatures.json", import.meta.url),
        "utf8",
      ),
    ) as { oligos: number; bases_sha256: string; sodium: (number | null)[] };
    const made = oligos();
    expect(made).toHaveLength(recorded.oligos);
    expect(createHash("sha256").update(made.join("\n")).digest("hex")).toBe(recorded.bases_sha256);
    const differing = made.flatMap((bases, at) => {
      const now = meltingTemperature(bases);
      const then = recorded.sodium[at];
      return now === then ? [] : [{ bases, now, then }];
    });
    expect(differing.slice(0, 10)).toEqual([]);
  });

  it("is SantaLucia's, as another implementation of it and the paper's own tables give it", () => {
    // SantaLucia 1998 (https://doi.org/10.1073/pnas.95.4.1460) by hand, for the T7 promoter
    // primer TAATACGACTCACTATAGGG. Table 2: its 19 stacks (TA ×4, AC ×3, AT ×2, CT ×2, GG ×2,
    // and AA, AG, CA, CG, GA, TC) and initiation at a terminal A·T and G·C give ΔH° =
    // −148.8 kcal/mol and ΔS° = −411.2 cal/(K·mol) in 1 M Na⁺. Equation 8 at 50 mM Na⁺
    // adds 0.368 × 19 × ln 0.05 = −20.946 to ΔS°; equation 3 with 25 nM of each strand,
    // C_T/4 = 12.5 nM: Tm = −148,800 / (−432.146 + 1.987 × ln 12.5e−9) − 273.15 = 44.59 °C.
    expect(meltingTemperature("TAATACGACTCACTATAGGG")).toBe(44.6);
    // Primer3's oligotm (primer3-py 2.4.0, calc_tm), a separate implementation of the same
    // model: tm_method=santalucia, salt_corrections_method=santalucia, mv_conc=50,
    // dv_conc=0, dntp_conc=0, dna_conc=50 (nM of strands in all). Unrounded, as it gives
    // them; Iki's are to 0.1 °C. A self-complementary strand is SantaLucia's C_T, all the
    // strands, with his symmetry term, as Primer3 has it (Biopython's Tm_NN, with
    // selfcomp, counts only dnac1, and gives 1 °C less for these).
    const primer3: [string, string, number][] = [
      ["SP6 promoter primer", "ATTTAGGTGACACTATAG", 38.934],
      ["T7 promoter primer", "TAATACGACTCACTATAGGG", 44.592],
      ["M13 reverse (−48)", "AGCGGATAACAATTTCACACAGGA", 54.839],
      ["AT-rich 25-mer", "AATTAAATATTTAAAATTATATTAA", 36.123],
      ["GC-rich 20-mer (95% GC)", "CGCGGCGGCGGCCGGCGCAG", 73.113],
      ["GC-rich 22-mer (95% GC)", "GCCGGCGCCCGCAGCCGCGGCC", 76.042],
      ["self-complementary 18-mer, EcoRI in it", "ACGTACGAATTCGTACGT", 49.324],
      ["self-complementary 18-mer, BamHI and NheI in it", "GCTAGCGGATCCGCTAGC", 55.553],
      ["self-complementary 20-mer", "GACGTCAGAATTCTGACGTC", 51.659],
      ["Dickerson–Drew dodecamer", "CGCGAATTCGCG", 43.763],
    ];
    for (const [name, bases, expected] of primer3) {
      const found = meltingTemperature(bases);
      expect.soft(found, name).not.toBeNull();
      // Rounding to one place, and nothing more.
      expect.soft(Math.abs((found ?? NaN) - expected), name).toBeLessThanOrEqual(0.05);
    }
  });

  it("matches the Rust module carried by the DNA plugin, including memory growth and invalid input", async () => {
    const binary = new URL("../wasm/oligo-measurement.wasm", import.meta.url);
    const { instance } = await WebAssembly.instantiate(await readFile(binary));
    const rust = instance.exports as unknown as ScalarModule;
    // Synthetic sequences cover ambiguity, self-complementarity and reused buffers.
    for (const input of [
      "",
      "A",
      "acggta",
      "GAATTC",
      "ACNG",
      "ÅT",
      "TACG".repeat(40_000),
      "ccgg",
    ]) {
      const bytes = new TextEncoder().encode(input);
      const at = rust.iki_input(bytes.byteLength);
      new Uint8Array(rust.memory.buffer, at, bytes.byteLength).set(bytes);
      const expected = rust.iki_melting_temperature_value();
      expect(meltingTemperature(input)).toBe(Number.isFinite(expected) ? expected : null);
    }
  });

  it("runs the synchronous calculation with no browser compiler or network", async () => {
    vi.resetModules();
    vi.stubGlobal("WebAssembly", undefined);
    vi.stubGlobal("fetch", () => {
      throw new Error("unexpected network request");
    });
    try {
      const { meltingTemperature: measure } = await import("./oligo-temperature.js");
      // SantaLucia (1998), table 2 and equations 3/8, as implemented by the shared Rust model.
      // https://doi.org/10.1073/pnas.95.4.1460
      expect(measure("GAATTC")).toBe(-20.4);
      expect(measure("")).toBeNull();
    } finally {
      vi.unstubAllGlobals();
    }
  });
});
