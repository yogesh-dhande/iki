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

  it("spreads a deployment's types over every swatch", () => {
    const types = [
      "iki/dna.Plasmid",
      "iki/dna.DnaSequence",
      "iki/dna.Primer",
      "iki/protein.Protein",
      "iki/protein.Antibody",
      "iki/protein.ProteinStructure",
      "iki/lims.Sample",
      "iki/lims.Instrument",
      "iki/lims.Booking",
      "iki/chem.Compound",
      "iki/chem.CompoundBatch",
      "iki/oligo.Oligonucleotide",
      "iki/oligo.Monomer",
      "iki/core.Collection",
      "iki/core.App",
      "iki/align.Alignment",
      "iki/organism.Organism",
      "iki/assembly.Assembly",
      "iki/requests.AnalysisRequest",
      "iki/requests.AnalysisResult",
      "iki/lots.GenericLot",
      "iki/lots.ProteinLot",
      "iki/lots.OligonucleotideLot",
    ];
    expect(new Set(types.map(typeSwatch)).size).toBe(8);
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
