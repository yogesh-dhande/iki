import { OligoDuplex } from "@ikisci/science/oligo-duplex";

class DuplexTab extends OligoDuplex {
  set entity(entity) {
    this.setAttribute(
      "empty-message",
      "Its pairing isn't found yet: it's found from its strands when it's registered.",
    );
    const pairing = entity?.values?.pairing;
    this.data = pairing
      ? {
          sense: pairing.sense?.bases ?? "",
          antisense: pairing.antisense?.bases ?? "",
          pairs: pairing.pairs ?? [],
        }
      : null;
  }
}
customElements.define("oligo-duplex", DuplexTab);
