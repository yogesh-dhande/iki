// @vitest-environment node
import { readFile } from "node:fs/promises";
import { describe, expect, it, vi } from "vitest";
import { meltingTemperature } from "./oligo-temperature.js";

interface ScalarModule {
  memory: WebAssembly.Memory;
  iki_input: (length: number) => number;
  iki_melting_temperature_value: () => number;
}

describe("the supplied DNA strand’s melting temperature", () => {
  it("matches the Rust module carried by the DNA plugin, including memory growth and invalid input", async () => {
    const binary = new URL("../../../../plugins/dna/data/oligo-measurement.wasm", import.meta.url);
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
