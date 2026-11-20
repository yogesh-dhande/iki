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
import { describeHelm, readHelm } from "./helm";
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
    const length = (n: number) => ({ fewest: n, most: n });
    expect(
      describeHelm({
        canonical: "",
        connections: [],
        groups: [],
        version1: false,
        polymers: [
          {
            id: "RNA1",
            type: "RNA",
            units: 21,
            length: length(21),
            monomers: [],
            ambiguous: false,
          },
          {
            id: "PEPTIDE1",
            type: "PEPTIDE",
            units: 1,
            length: length(1),
            monomers: [],
            ambiguous: false,
          },
        ],
      }),
    ).toBe("HELM: a strand of 21 nucleotides and a chain of 1 amino acid.");
  });

  /** Each string read by iki-wasm, then described. */
  async function described(...texts: string[]): Promise<string[]> {
    return (await readHelm(texts)).map(describeHelm);
  }

  it("says how many a range, an unknown span and a missing monomer stand for", async () => {
    // Pistoia's ambiguous monomers: G'2-5' is two to five glycines, * any number of
    // amino acids, X one unknown one, and _ a missing one, which adds nothing.
    expect(
      await described(
        "PEPTIDE1{A.G'2-5'.C}$$$$V2.0",
        "PEPTIDE1{A.*.G}$$$$V2.0",
        "PEPTIDE1{A._.G}$$$$V2.0",
        "PEPTIDE1{A.X.C}$$$$V2.0",
        "PEPTIDE1{H.E.L.M.(K,_)}$$$$V2.0",
      ),
    ).toEqual([
      "HELM: a chain of 4 to 7 amino acids.",
      "HELM: a chain of at least 2 amino acids.",
      "HELM: a chain of 2 amino acids.",
      "HELM: a chain of 3 amino acids, not all of them known.",
      "HELM: a chain of 4 to 5 amino acids.",
    ]);
  });

  it("doesn't count a 5′ phosphate as a nucleotide", async () => {
    expect(await described("RNA1{P.R(A)P.R(C)}$$$$V2.0")).toEqual([
      "HELM: a strand of 2 nucleotides and 1 other unit.",
    ]);
  });

  it("says a group of alternatives is one of them, and a mixture is all of them", async () => {
    // Pistoia's polymer groups: "The comma becomes an OR operator".
    expect(
      await described(
        "PEPTIDE1{A.C.D}|PEPTIDE2{P.Q}$$G1(PEPTIDE1,PEPTIDE2)$$V2.0",
        "PEPTIDE1{A.C.D}|PEPTIDE2{P.Q}$$G1(PEPTIDE1+PEPTIDE2)$$V2.0",
        "PEPTIDE1{A.C.D}|PEPTIDE2{P.Q}$$G1(PEPTIDE1:2+PEPTIDE2:3)$$V2.0",
      ),
    ).toEqual([
      "HELM: either a chain of 3 amino acids or a chain of 2 amino acids.",
      "HELM: a mixture of a chain of 3 amino acids and a chain of 2 amino acids.",
      "HELM: a mixture of a chain of 3 amino acids and a chain of 2 amino acids, 2:3.",
    ]);
  });

  it("counts a connection, not text in in-line SMILES", async () => {
    // HELM 2.04's in-line CXSMILES example: its atom labels hold "$".
    expect(await described("PEPTIDE1{A.[*N[C@@H](C)C(=O)* |$_R1;;;;;;_R2$|].G}$$$$V2.0")).toEqual([
      "HELM: a chain of 3 amino acids.",
    ]);
  });
});
