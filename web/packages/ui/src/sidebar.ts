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

/**
 * Each keyed control keeps its original DOM nodes across updates. Rebuilding a
 * control would detach the pressed SVG path between pointerdown and pointerup,
 * cancelling the click even if the new control looks identical.
 */
function createLink(host: IkiSidebar, href: string, headingAction: boolean) {
  const link = navigationLink(host, href);
  const graphic = icon([]);
  const mark = part("span", "");
  const label = part("span", headingAction ? "visually-hidden" : "nav-label");
  const qualifier = suffix("type-plugin");
  const count = suffix("nav-count");
  const badge = suffix("nav-badge");
  const description = suffix("visually-hidden");

  return (data: SidebarLink) => {
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
  };
}

function createDisclosure(host: IkiSidebar, id: string) {
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
    host.dispatchEvent(
      new CustomEvent<SidebarToggleDetail>("iki-toggle", {
        bubbles: true,
        composed: true,
        detail: { id, open },
      }),
    );
  });
  const graphic = icon([]);
  const name = part("span", "nav-label");
  const arrow = icon(["m9 6 6 6-6 6"]);
  return (
    label: string,
    disclosure: NonNullable<SidebarSection["disclosure"]>,
    links: HTMLAnchorElement[],
  ) => {
    setText(name, label);
    arrange(
      button,
      disclosure.icon ? [icon(disclosure.icon, graphic), name, arrow] : [name, arrow],
    );
    show(disclosure.open);
    arrange(items, links);
    return [button, items];
  };
}

function createGroup(label: string) {
  const group = part("div", "nav-group");
  group.role = "group";
  group.ariaLabel = label;
  const heading = part("div", "nav-heading");
  const text = part("span", "", label);
  text.ariaHidden = "true";
  return (links: HTMLAnchorElement[], action?: HTMLAnchorElement) => {
    arrange(heading, action ? [text, action] : [text]);
    arrange(group, [heading, ...links]);
    return group;
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
    const update = retain([headingAction ? "action" : "link", scope, data.href], () =>
      createLink(this, data.href, headingAction),
    );
    return update(data);
  }

  #section(section: SidebarSection, retain: Retain): HTMLElement[] {
    const { label, disclosure } = section;
    const scope = disclosure?.id ?? label ?? "";
    const nodes = section.gap ? [retain(["gap", scope], () => part("div", "nav-gap"))] : [];
    const links = section.links.map((link) => this.#link(link, scope, retain));
    if (!label) return [...nodes, ...links];
    if (disclosure) {
      const update = retain(["disclosure", disclosure.id], () =>
        createDisclosure(this, disclosure.id),
      );
      return [...nodes, ...update(label, disclosure, links)];
    }
    const update = retain(["group", label], () => createGroup(label));
    const action = section.action ? this.#link(section.action, scope, retain, true) : undefined;
    return [...nodes, update(links, action)];
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
