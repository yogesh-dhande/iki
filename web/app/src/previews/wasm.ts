/**
 * iki-previews' exports (worker/crates/iki-previews), and how one is called: the input is
 * written into a buffer the module gives, the export runs, and its JSON (and a TIFF tile's
 * pixels) are read back. The worker runs this; so do the tests, on the module itself.
 */

export interface Exports {
  memory: WebAssembly.Memory;
  iki_input: (length: number) => number;
  iki_output: () => number;
  iki_output_len: () => number;
  iki_pixels: () => number;
  iki_pixels_len: () => number;
  iki_abif: () => void;
  iki_jcamp: () => void;
  iki_mzml_offset: () => void;
  iki_mzml_index: () => void;
  iki_mzml_scan: (complete: number) => void;
  iki_mzml_element: (maxPoints: number) => void;
  iki_tiff_layout: () => void;
  iki_tiff_tile: (requestLength: number) => void;
  iki_csv: (delimiter: number, maxRows: number, maxColumns: number, complete: number) => void;
  iki_xlsx: (sheet: number, maxRows: number, maxColumns: number) => void;
}

export type Operation =
  | "abif"
  | "jcamp"
  | "mzml_offset"
  | "mzml_index"
  | "mzml_scan"
  | "mzml_element"
  | "tiff_layout"
  | "tiff_tile"
  | "csv"
  | "xlsx";

/** The most an input may be, as the module also checks. */
export const MAX_INPUT = 128 * 1024 * 1024;

const decoder = new TextDecoder();

/** Runs one export on `input`, with its numeric arguments. */
export function call(
  wasm: Exports,
  operation: Operation,
  args: readonly number[],
  input: Uint8Array,
): { json: string; pixels: ArrayBuffer | null } {
  if (input.byteLength > MAX_INPUT) throw new RangeError("The input is over 128 MiB.");
  const at = wasm.iki_input(input.byteLength);
  if (at === 0 && input.byteLength) throw new RangeError("The readers refused the input.");
  new Uint8Array(wasm.memory.buffer, at, input.byteLength).set(input);
  const run: (...a: number[]) => void = wasm[`iki_${operation}`];
  run(...args);
  // A call can grow memory and so replace its buffer: read the views after it.
  const json = decoder.decode(
    new Uint8Array(wasm.memory.buffer, wasm.iki_output(), wasm.iki_output_len()),
  );
  let pixels: ArrayBuffer | null = null;
  if (operation === "tiff_tile" && wasm.iki_pixels_len()) {
    pixels = new Uint8Array(wasm.memory.buffer, wasm.iki_pixels(), wasm.iki_pixels_len()).slice()
      .buffer;
  }
  return { json, pixels };
}
