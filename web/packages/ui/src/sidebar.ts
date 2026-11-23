import { icon, navigationLink, part } from "./navigation.js";

export type { NavigationDetail } from "./navigation.js";

export interface SidebarLink {
  label: string;
  href: string;
  current?: boolean;
  /** A suffix shown in muted text to distinguish links with the same label. */
  qualifier?: string | undefined;
  icon?: readonly string[] | undefined;
  mark?: { shape: "dot" | "square"; swatch: number };
  count?: number | null | undefined;
  badge?: { text: string; description: string } | undefined;
  quiet?: boolean;
}

export interface SidebarSection {
  label?: string;
  links: readonly SidebarLink[];
  /** Optional link on the group's heading, e.g. Create project. */
  action?: SidebarLink | undefined;
  /** Document-unique ID and initial open state of a collapsible group. */
  disclosure?: { id: string; open: boolean; icon?: readonly string[] };
  gap?: boolean;
}

export interface SidebarData {
  label: string;
  sections: readonly SidebarSection[];
}

export interface SidebarToggleDetail {
  id: string;
  open: boolean;
}

/** Scrolls the sidebar, keeping the active item and a row of surrounding context in view. */
export function reveal(list: HTMLElement, item: HTMLElement): void {
  const box = list.getBoundingClientRect();
  const at = item.getBoundingClientRect();
  const room = at.height;
  if (at.top < box.top) list.scrollTop -= box.top - at.top + room;
  else if (at.bottom > box.bottom) list.scrollTop += at.bottom - box.bottom + room;
}

export class IkiSidebar extends HTMLElement {
  readonly #nav = part("nav", "sidebar-nav");
  #data: SidebarData = { label: "Navigation", sections: [] };

  get data(): SidebarData {
    return this.#data;
  }

  set data(value: SidebarData) {
    this.#data = value;
    this.#render();
  }

  connectedCallback(): void {
    this.replaceChildren(this.#nav);
    this.#render();
  }

  #link(data: SidebarLink, headingAction = false): HTMLAnchorElement {
    const link = navigationLink(this, data.href);
    link.className = headingAction ? "nav-add" : data.quiet ? "nav-item nav-quiet" : "nav-item";
    if (data.current) link.setAttribute("aria-current", "page");
    if (headingAction) link.title = data.label;
    if (data.icon) link.append(icon(data.icon));
    if (data.mark) {
      const mark = part("span", data.mark.shape === "dot" ? "type-dot" : "swatch swatch-large");
      mark.setAttribute("aria-hidden", "true");
      mark.dataset["swatch"] = String(data.mark.swatch);
      link.append(mark);
    }
    const label = part("span", headingAction ? "visually-hidden" : "nav-label", data.label);
    if (data.qualifier) label.append(" ", part("span", "type-plugin", data.qualifier));
    link.append(label);
    if (data.count != null)
      link.append(" ", part("span", "nav-count", data.count.toLocaleString()));
    if (data.badge) {
      link.append(
        " ",
        part("span", "nav-badge", data.badge.text),
        " ",
        part("span", "visually-hidden", data.badge.description),
      );
    }
    return link;
  }

  #section(section: SidebarSection): DocumentFragment {
    const fragment = document.createDocumentFragment();
    if (section.gap) fragment.append(part("div", "nav-gap"));
    const links = section.links.map((link) => this.#link(link));
    const { label, disclosure } = section;
    if (!label) {
      fragment.append(...links);
    } else if (disclosure) {
      const button = part("button", "nav-item nav-toggle");
      button.type = "button";
      if (disclosure.icon) button.append(icon(disclosure.icon));
      button.append(part("span", "nav-label", label), icon(["m9 6 6 6-6 6"]));
      const items = part("div", "nav-sub");
      items.id = disclosure.id;
      button.setAttribute("aria-controls", items.id);
      const show = (open: boolean) => {
        items.hidden = !open;
        button.setAttribute("aria-expanded", String(open));
      };
      show(disclosure.open);
      button.addEventListener("click", () => {
        const open = items.hidden === true;
        show(open);
        this.dispatchEvent(
          new CustomEvent<SidebarToggleDetail>("iki-toggle", {
            bubbles: true,
            composed: true,
            detail: { id: disclosure.id, open },
          }),
        );
      });
      items.append(...links);
      fragment.append(button, items);
    } else {
      const group = part("div", "nav-group");
      group.setAttribute("role", "group");
      group.setAttribute("aria-label", label);
      const heading = part("div", "nav-heading");
      const text = part("span", "", label);
      text.setAttribute("aria-hidden", "true");
      heading.append(text);
      if (section.action) heading.append(this.#link(section.action, true));
      group.append(heading, ...links);
      fragment.append(group);
    }
    return fragment;
  }

  #render(): void {
    const active = this.ownerDocument.activeElement;
    const focus =
      active && this.#nav.contains(active)
        ? [active.tagName, active.getAttribute("href"), active.getAttribute("aria-controls")]
        : undefined;
    this.#nav.setAttribute("aria-label", this.#data.label);
    this.#nav.replaceChildren(...this.#data.sections.map((section) => this.#section(section)));
    if (focus) {
      const matching = Array.from(this.#nav.querySelectorAll<HTMLElement>("a,button")).find(
        (item) =>
          item.tagName === focus[0] &&
          item.getAttribute("href") === focus[1] &&
          item.getAttribute("aria-controls") === focus[2],
      );
      matching?.focus({ preventScroll: true });
    }
    const current = this.#nav.querySelector<HTMLElement>('[aria-current="page"]');
    if (current) reveal(this.#nav, current);
  }
}

if (!customElements.get("iki-sidebar")) customElements.define("iki-sidebar", IkiSidebar);

declare global {
  interface HTMLElementTagNameMap {
    "iki-sidebar": IkiSidebar;
  }
}
