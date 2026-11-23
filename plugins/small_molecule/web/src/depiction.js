// The frame bridge gives the adapter an entity. The public viewer accepts its plain depiction data.
/* global ChemicalDepiction */
class CompoundDepiction extends ChemicalDepiction {
  set entity(entity) {
    this.data = {
      smiles: entity?.values?.smiles ?? "",
      name: entity?.values?.name || undefined,
      label: entity?.hrid,
    };
  }
}
customElements.define("small-molecule-depiction", CompoundDepiction);
