import { PlasmidMap } from "@ikisci/science/plasmid-map";

const ANNOTATE = "iki/plasmid.annotate_plasmid";
const EMPTY_MESSAGE =
  "Its map isn't drawn yet: it's drawn once the plasmid is annotated from the library, by Annotate a plasmid from the library. A project can run it on each plasmid it gets, with its trigger Annotate each new plasmid from the library.";

function recorded(items, source) {
  const found = Array.isArray(items) ? items : [];
  return found.length || source ? { items: found, source } : undefined;
}

class PlasmidMapAdapter extends PlasmidMap {
  #entity;
  #place;
  #primers = [];

  constructor() {
    super();
    this.addEventListener("primer-open", ({ detail }) => {
      this.dispatchEvent(
        new CustomEvent("iki-open", { detail: { entity: detail.id }, bubbles: true }),
      );
    });
    this.addEventListener("empty-action", () => {
      if (this.#place?.runs?.includes(ANNOTATE)) {
        this.dispatchEvent(new CustomEvent("iki-run", { detail: { action: ANNOTATE } }));
      }
    });
  }
  set entity(entity) {
    this.#entity = entity;
    this.update();
  }
  set place(place) {
    this.#place = place;
    this.update();
  }
  set entities(entities) {
    this.#primers = entities?.items?.map((p) => p.hrid) ?? [];
    this.update();
  }
  update() {
    const values = this.#entity?.values ?? {};
    this.data = {
      name: values.name ?? this.#entity?.hrid ?? "",
      length: Number(values.length),
      topology: values.topology === "linear" ? "linear" : "circular",
      parts: recorded(values.annotated_features, values.annotated_with),
      orfs: recorded(values.orfs, values.orfs_found_with),
      sites: recorded(values.primer_binding_sites, values.primer_binding_sites_found_with),
      linkedPrimerIds: this.#primers,
      emptyMessage: EMPTY_MESSAGE,
      emptyAction: this.#place?.runs?.includes(ANNOTATE) ? "Run annotation" : undefined,
    };
  }
}

customElements.define("plasmid-map", PlasmidMapAdapter);
