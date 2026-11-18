// A test component that shows many proteins (ADR 0117): a card for each, with buttons
// that select or open it, a filter that narrows them by name, and one for more.
class Board extends HTMLElement {
  #entities = { items: [], more: false, limited: false, refinement: null };
  #place = { slot: "", label: "" };

  set place(place) {
    this.#place = place;
    this.#draw();
  }

  set entities(entities) {
    this.#entities = entities;
    this.#draw();
  }

  #send(name, detail) {
    this.dispatchEvent(new CustomEvent(name, { detail, bubbles: true }));
  }

  #draw() {
    const { items, more, limited, loading, error } = this.#entities;
    const status = document.createElement("p");
    status.textContent =
      `${this.#place.label}: ${items.length} shown` +
      (more ? ", more to come" : "") +
      (limited ? ", at its limit" : "") +
      (loading ? ", loading" : "") +
      (error ? `, ${error}` : "");
    const name = document.createElement("input");
    name.setAttribute("aria-label", "Name starts with");
    const filter = document.createElement("button");
    filter.textContent = "Filter";
    filter.addEventListener("click", () => {
      const value = name.value.trim();
      this.#send(
        "iki-refine",
        value ? { where: { field: "name", op: "starts_with", value } } : null,
      );
    });
    const cards = document.createElement("ul");
    for (const protein of items) {
      const card = document.createElement("li");
      const length = (protein.values.sequence || "").length;
      const dna = protein.refs[protein.values.encoded_by]?.hrid ?? "no DNA";
      card.append(`${protein.hrid} ${protein.values.name ?? ""} (${length} aa, ${dna})`);
      const pick = document.createElement("button");
      pick.textContent = `Select ${protein.hrid}`;
      pick.addEventListener("click", () => this.#send("iki-select", { entities: [protein.hrid] }));
      const open = document.createElement("button");
      open.textContent = `Open ${protein.hrid}`;
      open.addEventListener("click", () => this.#send("iki-open", { entity: protein.hrid }));
      card.append(" ", pick, " ", open);
      cards.append(card);
    }
    const parts = [status, name, filter, cards];
    if (more) {
      const next = document.createElement("button");
      next.textContent = "More";
      next.addEventListener("click", () => this.#send("iki-more"));
      parts.push(next);
    }
    this.replaceChildren(...parts);
  }
}
customElements.define("protein-board", Board);
