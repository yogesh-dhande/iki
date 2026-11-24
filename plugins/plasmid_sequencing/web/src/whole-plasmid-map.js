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

const UNPLACED = {
  unaligned: "align nowhere on the plasmid",
  repeated: "repeat bases already placed on the plasmid, such as a second lap of it",
};

/** The consensus's stretches that aren't placed on the plasmid once, in words: why the
 * result differs even where no single difference is listed. */
function unplacedNotes(values) {
  const total = Number(values.consensus_length);
  return list(values.unplaced).map((stretch) => {
    const where =
      stretch.start === stretch.end
        ? `base ${stretch.start.toLocaleString("en")}`
        : `bases ${stretch.start.toLocaleString("en")} to ${stretch.end.toLocaleString("en")}`;
    const of = Number.isFinite(total) ? ` of its ${total.toLocaleString("en")}` : "";
    return `The consensus's ${where}${of} ${UNPLACED[stretch.why] ?? "aren't placed once"}.`;
  });
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
      notes: unplacedNotes(values),
      emptyMessage: VERDICT[values.agreement] ?? "Nothing was compared.",
    };
  }
}

customElements.define("whole-plasmid-map", WholePlasmidMap);
