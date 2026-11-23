import {
  arrange,
  icon,
  NavigationItems,
  navigationLink,
  part,
  setText,
  type Retain,
} from "./navigation.js";

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

/** Preserve the separating space in a control's visible and accessible name. */
function suffix(className: string) {
  const nodes = [new Text(" "), part("span", className)] as const;
  return (text: string) => {
    setText(nodes[1], text);
    return nodes;
  };
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
    const { link, graphic, mark, label, qualifier, count, badge, description } = retain(
      key,
      () => ({
        link: navigationLink(this, data.href),
        graphic: icon([]),
        mark: part("span", ""),
        label: part("span", headingAction ? "visually-hidden" : "nav-label"),
        qualifier: suffix("type-plugin"),
        count: suffix("nav-count"),
        badge: suffix("nav-badge"),
        description: suffix("visually-hidden"),
      }),
    );
    link.className = headingAction ? "nav-add" : data.quiet ? "nav-item nav-quiet" : "nav-item";
    link.ariaCurrent = data.current ? "page" : null;
    if (headingAction) link.title = data.label;

    const children: Node[] = data.icon ? [icon(data.icon, graphic)] : [];
    if (data.mark) {
      mark.className = data.mark.shape === "dot" ? "type-dot" : "swatch swatch-large";
      mark.ariaHidden = "true";
      mark.dataset["swatch"] = String(data.mark.swatch);
      children.push(mark);
    }
    const name = setText(label, data.label);
    arrange(label, data.qualifier ? [name, ...qualifier(data.qualifier)] : [name]);
    children.push(label);
    if (data.count != null) children.push(...count(data.count.toLocaleString()));
    if (data.badge) {
      children.push(...badge(data.badge.text), ...description(data.badge.description));
    }
    arrange(link, children);
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
      const { button, items, show, graphic, name, arrow } = retain(["disclosure", id], () => {
        const button = part("button", "nav-item nav-toggle");
        button.type = "button";
        button.setAttribute("aria-controls", id);
        const items = part("div", "nav-sub");
        items.id = id;
        const show = (open: boolean) => {
          items.hidden = !open;
          button.ariaExpanded = String(open);
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
        return {
          button,
          items,
          show,
          graphic: icon([]),
          name: part("span", "nav-label"),
          arrow: icon(["m9 6 6 6-6 6"]),
        };
      });
      setText(name, label);
      arrange(
        button,
        disclosure.icon ? [icon(disclosure.icon, graphic), name, arrow] : [name, arrow],
      );
      show(disclosure.open);
      arrange(items, links);
      return [...nodes, button, items];
    }
    const { group, heading, text } = retain(["group", label], () => {
      const group = part("div", "nav-group");
      group.role = "group";
      group.ariaLabel = label;
      const heading = part("div", "nav-heading");
      const text = part("span", "", label);
      text.ariaHidden = "true";
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
    this.#nav.ariaLabel = this.#data.label;
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
