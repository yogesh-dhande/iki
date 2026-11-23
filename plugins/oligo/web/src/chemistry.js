// The frame receives only the positions the viewer can read (ADR 0084).
import { OligoChemistry } from "@ikisci/science/oligo-chemistry";

class ChemistryTab extends OligoChemistry {
  set entity(entity) {
    this.data = {
      nucleotides: [],
      ...entity?.values?.positions,
      emptyMessage: `${entity?.hrid ?? "It"} has no positions computed yet: its properties are computed from its monomers when it's registered.`,
    };
  }
}
customElements.define("oligo-chemistry", ChemistryTab);
