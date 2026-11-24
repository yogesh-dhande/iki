import { instantiate } from "../generated/oligo-temperature.js";

let instance: ReturnType<typeof instantiate> | undefined;

/** The fixed-condition melting temperature (°C) of a supplied, unmodified DNA strand
 * with its exact complement. Rust owns the SantaLucia (1998) model and the rounding.
 * https://doi.org/10.1073/pnas.95.4.1460 */
export function meltingTemperature(bases: string): number | null {
  if (bases.length < 2 || bases.length > 64 * 1024 * 1024) return null;
  instance ??= instantiate({});
  const offset = instance.iki_input(bases.length);
  if (!offset) return null;
  // A valid strand is ASCII, so one UTF-16 code unit occupies one input byte.
  // Encode directly into the module’s reusable buffer without copying a long strand.
  const bytes = new Uint8Array(instance.memory.buffer, offset, bases.length);
  for (let i = 0; i < bases.length; i++) {
    const code = bases.charCodeAt(i);
    if (code > 0x7f) return null;
    bytes[i] = code;
  }
  const celsius = instance.iki_melting_temperature_value();
  return Number.isFinite(celsius) ? celsius : null;
}
