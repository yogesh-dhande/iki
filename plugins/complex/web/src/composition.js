import { ComplexComposition } from "@ikisci/science/complex-composition";
import * as iki from "@ikisci/component";

const COMPUTE = "iki/complex.compute_composition";

// Only labels supplied by the host may appear in the frame (ADR 0084).
function partName(refs, id) {
  const ref = refs?.[id];
  if (!ref) return "a part you can't read";
  return ref.title && ref.title !== ref.hrid ? `${ref.hrid} · ${ref.title}` : ref.hrid;
}

class CompositionTab extends ComplexComposition {
  #canCompute = false;

  constructor() {
    super();
    this.addEventListener("compute-request", () => {
      if (this.#canCompute) iki.run(this, COMPUTE);
    });
  }

  set place(place) {
    this.#canCompute = place?.runs?.includes(COMPUTE) ?? false;
    this.data = { ...this.data, canCompute: this.#canCompute };
  }

  set entity(entity) {
    const values = entity?.values ?? {};
    const name = (id) => partName(entity?.refs, id);
    this.data = {
      parts: (values.components ?? []).map(({ part, copies, role }) => ({
        name: name(part),
        copies,
        role,
      })),
      bonds: (values.bonds ?? []).map((b) => ({
        kind: b.kind,
        from: { part: name(b.part), copy: b.copy, residue: b.residue },
        to: { part: name(b.partner), copy: b.partner_copy, residue: b.partner_residue },
      })),
      reduced: values.molecular_formula
        ? {
            formula: values.molecular_formula,
            average: values.molecular_weight ?? null,
            monoisotopic: values.monoisotopic_mass ?? null,
            extinctionCoefficient: values.extinction_coefficient_reduced ?? null,
          }
        : null,
      canCompute: this.#canCompute,
      chainCount: values.chain_count,
      bondMap: values.bond_map,
      partDisulfides: values.part_disulfides,
      partBondsComplete: values.part_bonds_complete,
      uncountedPartsMessage: "its parts' own disulfides aren't counted in this project yet",
      emptyMessage: `${entity?.hrid ?? "It"} has no formula yet. Compute a complex's composition works it out from its parts; a project can run it on each complex it gets, with its trigger Compute each new complex's composition.`,
    };
  }
}

customElements.define("complex-composition", CompositionTab);
