/**
 * HELM fields (ADR 0144) in forms: read as they're typed with the real iki-wasm, run here
 * without a worker, saying what a string holds or what's wrong and at which character.
 */

import type { Schemas } from "@ikisci/sdk";
import { screen } from "@testing-library/react";
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it, vi } from "vitest";

import { field, KINASE, PROTEIN, renderApp } from "./entity-fixtures";
import { describeHelm } from "./helm";
import { answer, page, signedIn } from "./test-server";

vi.mock("./apps", async (original) => {
  const real = await original<typeof import("./apps")>();
  // Built by scripts/build-wasm.sh, as the TypeScript job does before the tests.
  const here = join(process.cwd(), "wasm", "iki_wasm.wasm");
  const bytes = readFileSync(existsSync(here) ? here : join("web", "app", "wasm", "iki_wasm.wasm"));
  const { instance } = await WebAssembly.instantiate(bytes, {});
  const wasm = instance.exports as unknown as {
    memory: WebAssembly.Memory;
    iki_input: (len: number) => number;
    iki_helm: () => void;
    iki_output: () => number;
    iki_output_len: () => number;
  };
  return {
    ...real,
    expressionRenderer: () => ({
      helm: (request: string) => {
        const sent = new TextEncoder().encode(request);
        const at = wasm.iki_input(sent.length);
        new Uint8Array(wasm.memory.buffer, at, sent.length).set(sent);
        wasm.iki_helm();
        const out = new Uint8Array(wasm.memory.buffer, wasm.iki_output(), wasm.iki_output_len());
        const found = JSON.parse(new TextDecoder().decode(out)) as { readings: unknown };
        return Promise.resolve(found.readings);
      },
    }),
  };
});

const OLIGO: Schemas["EntityTypeOut"] = {
  ...PROTEIN,
  name: "acme/oligo.Oligo",
  plugin: "acme/oligo",
  label: "Oligo",
  label_plural: "Oligos",
  description: null,
  hrid_prefix: "OLI",
  identity: [],
  values_schema: {
    type: "object",
    properties: { helm: { type: "string", "x-iki-format": "helm" } },
  },
  fields: [field({ name: "helm", label: "HELM", type: "string", plugin: "acme/oligo" })],
  rules: [],
};

describe("HELM in forms", () => {
  it("says what's wrong and where as it's typed, then what it holds", async () => {
    signedIn();
    answer("GET /api/v1/entity-types", [PROTEIN, OLIGO]);
    answer("GET /api/v1/projects", page([KINASE]));
    const { user } = await renderApp("/projects/kinase-atlas/entities/new/?type=acme/oligo.Oligo");
    const input = screen.getByRole("textbox", { name: /^HELM/ });
    await user.click(input);
    await user.paste("RNA1{[dR](A)[sP].}$$$$V2.0");
    expect(
      await screen.findByText("At character 18: Expected a monomer, but found “}”."),
    ).toBeInTheDocument();
    expect(input).toHaveAttribute("aria-invalid", "true");

    await user.clear(input);
    await user.paste("RNA1{[dR](A)[sP].[dR](C)}|CHEM1{[5Bio]}$RNA1,CHEM1,1:R1-1:R2$$$V2.0");
    expect(
      await screen.findByText("HELM: a strand of 2 nucleotides and 5Bio, with 1 connection."),
    ).toBeInTheDocument();
    expect(input).not.toHaveAttribute("aria-invalid");
  });

  it("describes duplexes and peptides", () => {
    expect(
      describeHelm({
        canonical: "",
        connections: [],
        version1: false,
        polymers: [
          { id: "RNA1", type: "RNA", units: 21, monomers: [], ambiguous: false },
          { id: "PEPTIDE1", type: "PEPTIDE", units: 1, monomers: [], ambiguous: false },
        ],
      }),
    ).toBe("HELM: a strand of 21 nucleotides and a chain of 1 amino acid.");
  });
});
