// A test component: the protein's length, and a button that selects it.
class Residues extends HTMLElement {
  set entity(entity) {
    const sequence = entity.values.sequence || "";
    const text = document.createElement("p");
    text.textContent = `${entity.hrid}: ${sequence.length} residues`;
    const pick = document.createElement("button");
    pick.textContent = "Select";
    pick.addEventListener("click", () =>
      this.dispatchEvent(
        new CustomEvent("iki-select", { detail: { entities: [entity.id] }, bubbles: true }),
      ),
    );
    this.replaceChildren(text, pick);
  }
}
customElements.define("protein-residues", Residues);
