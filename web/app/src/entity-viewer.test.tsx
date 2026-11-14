/** Iki's own viewers on an entity's page: the sequence viewer, its track and its features. */

import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { entity, field, PROTEIN, renderApp, withTypes } from "./entity-fixtures";
import { answer } from "./test-server";

const PLASMID = {
  ...PROTEIN,
  name: "dna.DNASequence",
  plugin: "dna",
  label: "DNA sequence",
  label_plural: "DNA sequences",
  hrid_prefix: "DNA",
  fields: [
    field({ name: "bases", label: "Bases", type: "string" }),
    field({ name: "features", label: "Features", type: "json" }),
  ],
  viewers: [
    {
      viewer: "sequence" as const,
      label: "Sequence",
      sequence: "bases",
      alphabet: "dna" as const,
      features: "features",
    },
  ],
};

const BASES = "ACGT".repeat(40); // 160 bp

function onPlasmid(features: unknown) {
  withTypes();
  answer("GET /api/v1/entity-types", [PROTEIN, PLASMID]);
  answer(
    "GET /api/v1/entities/DNA-000001",
    entity({
      hrid: "DNA-000001",
      type: "dna.DNASequence",
      values: { name: "pTest", bases: BASES, features },
    }),
  );
}

describe("the sequence viewer", () => {
  it("is a tab with the sequence in rows, its features along it and in a table", async () => {
    onPlasmid([
      { type: "CDS", name: "tet", start: 5, end: 130, strand: "forward" },
      { type: "promoter", name: "P1", start: 150, end: 20, strand: "reverse" },
    ]);
    const { user } = await renderApp("/entities/DNA-000001/viewers/sequence/");
    expect(await screen.findByText("160 bp · 2 features")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Sequence" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("list", { name: "Feature kinds" })).toHaveTextContent(
      "CodingRegulatory",
    );
    const table = screen.getByRole("table");
    expect(
      within(table)
        .getAllByRole("row")
        .map((r) => r.textContent),
    ).toEqual([
      "NameTypeLocationStrandLengthShow",
      "tetCDS5..130forward126Show",
      "P1promoter150..20 (across the origin)reverse31Show",
    ]);
    await user.click(screen.getByRole("button", { name: "Show tet" }));
    expect(screen.getByRole("button", { name: "Hide tet" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("leaves out features it can't draw", async () => {
    onPlasmid([{ type: "CDS", start: 0, end: 500 }, "junk"]);
    await renderApp("/entities/DNA-000001/viewers/sequence/");
    expect(await screen.findByText("160 bp · 0 features")).toBeInTheDocument();
    expect(screen.getByText("It has no features yet.")).toBeInTheDocument();
  });

  it("isn't a tab of types without it", async () => {
    withTypes();
    answer("GET /api/v1/entities/PRT-000001", entity());
    await renderApp("/entities/PRT-000001/viewers/sequence/");
    expect(await screen.findByRole("heading", { name: "Not found" })).toBeInTheDocument();
  });
});
