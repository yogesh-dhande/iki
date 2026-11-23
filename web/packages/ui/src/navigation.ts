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

export function icon(paths: readonly string[]): SVGSVGElement {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("class", "icon");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  for (const d of paths) {
    const path = document.createElementNS(ns, "path");
    path.setAttribute("d", d);
    svg.append(path);
  }
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
export function place(
  parent: HTMLElement,
  child: HTMLElement,
  before: Element | null = null,
): void {
  if (child.isConnected && parent.isConnected && typeof parent.moveBefore === "function") {
    parent.moveBefore(child, before);
  } else {
    const active = child.ownerDocument.activeElement;
    parent.insertBefore(child, before);
    if (active instanceof HTMLElement && child.contains(active))
      active.focus({ preventScroll: true });
  }
}

/** Update the package-owned element children without detaching controls that are already in place. */
export function arrange(parent: HTMLElement, children: HTMLElement[]): void {
  const keep = new Set<Element>(children);
  for (const child of Array.from(parent.children)) {
    if (!keep.has(child)) child.remove();
  }
  let before = parent.firstElementChild;
  for (const child of children) {
    if (child === before) before = child.nextElementSibling;
    else place(parent, child, before);
  }
}
