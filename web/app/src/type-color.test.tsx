/**
 * Types' colors (ADR 0176): each type's dot is one of the eight swatches, from its name,
 * the same everywhere: the server's sidebar (iki.js) works it out the same way.
 */

import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { swatchOf } from "./project-color";
import { TypeDot, TypeName, typeSwatch } from "./type-color";

describe("a type's color", () => {
  it("is one of the swatches, from its name, as a project's is from its ID", () => {
    expect(typeSwatch("iki/dna.Plasmid")).toBe(swatchOf("iki/dna.Plasmid"));
    // The same numbers iki.js's swatchOf gives (test_web_app checks the server's).
    expect(["iki/dna.Plasmid", "iki/dna.Primer", "acme/protein.Protein"].map(typeSwatch)).toEqual(
      ["iki/dna.Plasmid", "iki/dna.Primer", "acme/protein.Protein"].map(swatchOf),
    );
  });

  it("spreads a deployment's types over every swatch, none crowded", () => {
    // The dev deployment's types: many share a prefix, which plain FNV-1a crowds together.
    const types = [
      "iki/core.Alignment",
      "iki/example_lims.AnalysisRequest",
      "iki/example_lims.AnalysisResult",
      "iki/antibody.Antibody",
      "iki/core.App",
      "iki/plasmid.Assembly",
      "iki/example_lims.Booking",
      "iki/core.Collection",
      "iki/small_molecule.Compound",
      "iki/small_molecule.CompoundBatch",
      "iki/dna.DNASequence",
      "iki/lots.GenericLot",
      "iki/example_lims.Instrument",
      "iki/oligo.Monomer",
      "iki/oligo.Oligonucleotide",
      "iki/lots.OligoLot",
      "iki/core.Organism",
      "iki/plasmid.Plasmid",
      "iki/dna.Primer",
      "iki/protein.Protein",
      "iki/lots.ProteinLot",
      "iki/protein.ProteinStructure",
      "acme/example.Sample",
      "iki/example_lims.Sample",
    ];
    const counts = new Map<number, number>();
    for (const t of types) counts.set(typeSwatch(t), (counts.get(typeSwatch(t)) ?? 0) + 1);
    expect(counts.size).toBe(8);
    expect(Math.max(...counts.values())).toBeLessThanOrEqual(6);
  });

  it("draws a dot named by its swatch, which screen readers skip", () => {
    const { container } = render(<TypeName type="iki/dna.Plasmid" label="Plasmid" />);
    const dot = container.querySelector(".type-dot");
    expect(dot).toHaveAttribute("data-swatch", String(typeSwatch("iki/dna.Plasmid")));
    expect(dot).toHaveAttribute("aria-hidden", "true");
    expect(container).toHaveTextContent("Plasmid");
  });

  it("keeps its own color in a neighbour's place", () => {
    const { container } = render(<TypeDot type="iki/dna.Primer" className="search-dot" large />);
    expect(container.firstChild).toHaveClass("type-dot", "type-dot-large", "search-dot");
  });
});
