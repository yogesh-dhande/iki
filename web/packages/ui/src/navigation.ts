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
