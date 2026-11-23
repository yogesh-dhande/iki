import { arrange, icon, NavigationItems, navigationLink, part, type Retain } from "./navigation.js";

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
  readonly #items = new NavigationItems();
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

  #link(
    data: SidebarLink,
    scope: string,
    retain: Retain,
    headingAction = false,
  ): HTMLAnchorElement {
    const key = [headingAction ? "action" : "link", scope, data.href];
    const link = retain(key, () => navigationLink(this, data.href));
    link.replaceChildren();
    link.className = headingAction ? "nav-add" : data.quiet ? "nav-item nav-quiet" : "nav-item";
    if (data.current) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
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

  #section(section: SidebarSection, retain: Retain): HTMLElement[] {
    const { label, disclosure } = section;
    const scope = disclosure?.id ?? label ?? "";
    const nodes = section.gap ? [retain(["gap", scope], () => part("div", "nav-gap"))] : [];
    const links = section.links.map((link) => this.#link(link, scope, retain));
    if (!label) return [...nodes, ...links];
    if (disclosure) {
      const { id } = disclosure;
      const { button, items, show } = retain(["disclosure", id], () => {
        const button = part("button", "nav-item nav-toggle");
        button.type = "button";
        button.setAttribute("aria-controls", id);
        const items = part("div", "nav-sub");
        items.id = id;
        const show = (open: boolean) => {
          items.hidden = !open;
          button.setAttribute("aria-expanded", String(open));
        };
        button.addEventListener("click", () => {
          const open = items.hidden === true;
          show(open);
          this.dispatchEvent(
            new CustomEvent<SidebarToggleDetail>("iki-toggle", {
              bubbles: true,
              composed: true,
              detail: { id, open },
            }),
          );
        });
        return { button, items, show };
      });
      button.replaceChildren();
      if (disclosure.icon) button.append(icon(disclosure.icon));
      button.append(part("span", "nav-label", label), icon(["m9 6 6 6-6 6"]));
      show(disclosure.open);
      arrange(items, links);
      return [...nodes, button, items];
    }
    const { group, heading, text } = retain(["group", label], () => {
      const group = part("div", "nav-group");
      group.setAttribute("role", "group");
      group.setAttribute("aria-label", label);
      const heading = part("div", "nav-heading");
      const text = part("span", "", label);
      text.setAttribute("aria-hidden", "true");
      return { group, heading, text };
    });
    arrange(
      heading,
      section.action ? [text, this.#link(section.action, scope, retain, true)] : [text],
    );
    arrange(group, [heading, ...links]);
    return [...nodes, group];
  }

  #render(): void {
    this.#nav.setAttribute("aria-label", this.#data.label);
    const retain = this.#items.next();
    arrange(
      this.#nav,
      this.#data.sections.flatMap((section) => this.#section(section, retain)),
    );
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
