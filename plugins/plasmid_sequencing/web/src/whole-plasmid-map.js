import { PlasmidMap } from "@ikisci/science/plasmid-map";

const VERDICT = {
  matches: "The consensus matches the plasmid's record at every base.",
  differs: "The consensus differs from the plasmid's record: each difference is below.",
  incomplete:
    "Nothing differs where the consensus is called, but some of the plasmid isn't called.",
};

function list(value) {
  return Array.isArray(value) ? value : [];
}

/** A whole-plasmid alignment drawn on its plasmid's map: the depth of reads round the
 * circle, each difference from the record, and the features they were placed in, as the
 * comparison recorded them. */
class WholePlasmidMap extends PlasmidMap {
  #entity;

  set entity(entity) {
    this.#entity = entity;
    this.update();
  }
  update() {
    const values = this.#entity?.values ?? {};
    const length = Number(values.reference_length);
    const reads = values.source === "reads";
    const coverage = values.coverage;
    this.data = {
      // The result's HRID in the middle of the ring, where a long name wouldn't fit.
      name: this.#entity?.hrid ?? "",
      length,
      topology: values.topology === "linear" ? "linear" : "circular",
      parts: {
        items: list(values.features),
        own: true,
        source: "The plasmid's record and annotation, when compared",
      },
      coverage:
        coverage && Array.isArray(coverage.mean)
          ? {
              ...coverage,
              minDepth: Number(values.min_depth) || undefined,
              meanDepth: reads ? values.mean_depth : undefined,
              leastDepth: reads ? values.least_depth : undefined,
              low: list(values.low_coverage),
              source: values.method,
            }
          : undefined,
      differences: { items: list(values.differences) },
      emptyMessage: VERDICT[values.agreement] ?? "Nothing was compared.",
    };
  }
}

customElements.define("whole-plasmid-map", WholePlasmidMap);
