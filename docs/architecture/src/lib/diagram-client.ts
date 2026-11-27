// Hover or focus previews a box's details; click or Enter keeps them. Leaving a box goes
// back to the one kept, so moving the pointer down to the details doesn't lose them.
for (const figure of document.querySelectorAll<HTMLElement>("[data-dg]")) {
  figure.classList.add("js");
  const nodes = [...figure.querySelectorAll<SVGGElement>("[data-node]")];
  const panels = new Map(
    [...figure.querySelectorAll<HTMLElement>(".dg-detail")].map((p) => [p.dataset.for, p]),
  );
  // Lines that name their two ends (the ADR graph's arcs) light up with either end.
  const lines = [...figure.querySelectorAll<SVGElement>("[data-ends]")];
  let kept: string | undefined;
  const show = (id: string | undefined): void => {
    for (const [key, panel] of panels) panel.classList.toggle("shown", key === id);
    for (const n of nodes) n.classList.toggle("current", n.dataset.node === id);
    const near = new Set<string>();
    for (const line of lines) {
      const ends = (line.dataset.ends ?? "").split(" ");
      const hot = id !== undefined && ends.includes(id);
      line.classList.toggle("hot", hot);
      if (hot) for (const end of ends) near.add(end);
    }
    if (lines.length) {
      figure.classList.toggle("focusing", id !== undefined);
      for (const n of nodes) n.classList.toggle("near", near.has(n.dataset.node ?? ""));
    }
    const hint = figure.querySelector<HTMLElement>(".dg-hint");
    if (hint) hint.hidden = id !== undefined;
  };
  for (const node of nodes) {
    const id = node.dataset.node;
    node.addEventListener("mouseenter", () => {
      show(id);
    });
    node.addEventListener("mouseleave", () => {
      show(kept);
    });
    node.addEventListener("focus", () => {
      show(id);
    });
    node.addEventListener("blur", () => {
      show(kept);
    });
    const keep = (): void => {
      kept = id;
      show(id);
    };
    node.addEventListener("click", keep);
    node.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        keep();
      }
    });
  }
  show(undefined);
}
