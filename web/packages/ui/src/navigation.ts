/** Link targets in component data. Application pages and external HTTPS/HTTP links are valid. */
export function navigationHref(href: string): string | undefined {
  try {
    const url = new URL(href, document.baseURI);
    if (url.protocol === "http:" || url.protocol === "https:") return href;
  } catch {
    // Invalid URLs render as text.
  }
  return undefined;
}

export interface NavigationDetail {
  href: string;
}

/** Keep browser defaults for modified clicks and let routers handle an ordinary activation. */
export function navigationLink(host: HTMLElement, href: string): HTMLAnchorElement {
  const link = document.createElement("a");
  const safeHref = navigationHref(href);
  if (safeHref === undefined) return link;
  link.setAttribute("href", safeHref);
  link.addEventListener("click", (click) => {
    if (click.button || click.ctrlKey || click.metaKey || click.altKey || click.shiftKey) return;
    const navigate = new CustomEvent<NavigationDetail>("iki-navigate", {
      detail: { href: safeHref },
      bubbles: true,
      composed: true,
      cancelable: true,
    });
    if (!host.dispatchEvent(navigate)) click.preventDefault();
  });
  return link;
}

export function part<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  className: string,
  text?: string,
): HTMLElementTagNameMap[K] {
  const element = document.createElement(tag);
  element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

/** Update a label without replacing the text node selected by the browser. */
export function setText(element: Element, value: string): Text {
  const node = element.firstChild;
  if (!(node instanceof Text)) return element.insertBefore(new Text(value), node);
  if (node.data !== value) node.data = value;
  return node;
}

const SVG_NS = "http://www.w3.org/2000/svg";

/** Update path data in place so a pointer held on an icon keeps its target. */
export function icon(
  paths: readonly string[],
  svg = document.createElementNS(SVG_NS, "svg"),
): SVGSVGElement {
  svg.setAttribute("class", "icon");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  paths.forEach((d, i) => {
    const path = svg.children[i] ?? svg.appendChild(document.createElementNS(SVG_NS, "path"));
    if (path.getAttribute("d") !== d) path.setAttribute("d", d);
  });
  while (svg.children.length > paths.length) svg.lastElementChild?.remove();
  return svg;
}

/** Reuse the controls with the same keys in the next render, including repeated link targets. */
export type Retain = <T extends object>(key: readonly string[], create: () => T) => T;

export class NavigationItems {
  #items = new Map<string, object[]>();

  next(): Retain {
    const previous = this.#items;
    const current = (this.#items = new Map<string, object[]>());
    return <T extends object>(key: readonly string[], create: () => T): T => {
      const id = JSON.stringify(key);
      // The first key part names the control type. Duplicate keys match in display order.
      const item = (previous.get(id)?.shift() as T | undefined) ?? create();
      const matches = current.get(id);
      if (matches) matches.push(item);
      else current.set(id, [item]);
      return item;
    };
  }
}

/** Preserve focus when a breakpoint or new ordering moves a control or one of its ancestors. */
export function place(parent: HTMLElement, child: Node, before: Node | null = null): void {
  if (child.isConnected && parent.isConnected && typeof parent.moveBefore === "function") {
    parent.moveBefore(child, before);
  } else {
    const active = parent.ownerDocument.activeElement;
    parent.insertBefore(child, before);
    if (active instanceof HTMLElement && child.contains(active))
      active.focus({ preventScroll: true });
  }
}

/** Update package-owned children without detaching nodes that are already in place. */
export function arrange(parent: HTMLElement, children: readonly Node[]): void {
  const keep = new Set<Node>(children);
  for (const child of Array.from(parent.childNodes)) {
    if (!keep.has(child)) child.remove();
  }
  let before = parent.firstChild;
  for (const child of children) {
    if (child === before) before = before.nextSibling;
    else place(parent, child, before);
  }
}
