// All externally supplied labels are text nodes, including formulae and monomer names.
export function element<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  attributes: Record<string, string> = {},
  ...children: (string | Node)[]
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(attributes)) node.setAttribute(name, value);
  node.append(...children);
  return node;
}

export interface DetailsChange {
  section: "positions" | "strands" | "bonds";
  open: boolean;
}

/** Native disclosures work with the keyboard. Forward their user-visible change through the shadow root. */
export function disclosure(
  section: DetailsChange["section"],
  title: string,
  contents: Node,
  open = false,
) {
  const details = element("details", {}, element("summary", {}, title), contents);
  details.open = open;
  let previous = open;
  details.addEventListener("toggle", () => {
    if (details.open === previous) return;
    previous = details.open;
    details.dispatchEvent(
      new CustomEvent<DetailsChange>("details-change", {
        detail: { section, open: details.open },
        bubbles: true,
        composed: true,
      }),
    );
  });
  return details;
}
