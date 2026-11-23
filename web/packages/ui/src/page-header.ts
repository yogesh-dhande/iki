import { icon, navigationLink, part } from "./navigation.js";

export type { NavigationDetail } from "./navigation.js";

export interface HeaderLink {
  label: string;
  href: string;
}

export interface PageHeaderData {
  title: string;
  eyebrow?: string;
  badge?: string;
  description?: string;
  about?: HeaderLink;
  /** Links to ancestors, in order. The current title is appended to the trail. */
  ancestors?: readonly HeaderLink[];
  actions?: readonly { id: string; label: string }[];
}

export interface HeaderActionDetail {
  id: string;
}

let nextId = 0;
export const PHONE = "(max-width: 36rem)";

/** Slots in the light DOM let a framework supply actions, links and richer descriptions. */
export class IkiPageHeader extends HTMLElement {
  readonly heading = part("h1", "");
  readonly areas = {
    eyebrow: part("p", "eyebrow"),
    marker: part("span", "page-marker"),
    badge: part("span", "page-badge"),
    description: part("p", "page-description-text"),
    fullDescription: part("p", ""),
    about: part("div", ""),
    actions: part("div", "page-actions page-actions-below"),
  };
  readonly #headingText = new Text();
  readonly #eyebrowText = new Text();
  readonly #badgeText = new Text();
  readonly #descriptionText = new Text();
  readonly #fullText = new Text();
  readonly #row = part("div", "page-title-row");
  readonly #description = part("div", "page-description");
  readonly #button = part("button", "page-about");
  readonly #popover = part("div", "page-about-popover");
  readonly #dialog = part("div", "page-about-dialog");
  readonly #aboutLink = part("div", "");
  readonly #bar = part("div", "page-top page-top-inline");
  readonly #actionBar = part("div", "page-actions");
  #data: PageHeaderData = { title: "" };
  #resize?: ResizeObserver;
  #mutations?: MutationObserver;
  #media: MediaQueryList | undefined;

  constructor() {
    super();
    this.heading.tabIndex = -1;
    this.heading.dataset["pageTitle"] = "";
    this.heading.append(this.#headingText, this.areas.badge);
    this.areas.eyebrow.append(this.#eyebrowText);
    this.areas.badge.append(this.#badgeText);
    this.areas.description.append(this.#descriptionText);
    this.areas.fullDescription.append(this.#fullText);
    this.#button.type = "button";
    this.#button.setAttribute("aria-haspopup", "dialog");
    this.#button.setAttribute("aria-expanded", "false");
    this.#button.append(icon(["M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z", "M12 11v5", "M12 7.5v.01"]));
    this.#popover.id = `iki-about-${String(++nextId)}`;
    this.#button.setAttribute("aria-controls", this.#popover.id);
    this.#dialog.setAttribute("role", "dialog");
    this.#dialog.tabIndex = -1;
    this.#dialog.append(this.areas.fullDescription, this.#aboutLink, this.areas.about);
    this.#popover.hidden = true;
    this.#description.append(this.areas.description, this.#button, this.#popover);
    this.#row.append(this.areas.marker, this.heading, this.#description);
    this.#button.onclick = () => {
      if (!this.#popover.hidden) this.#close();
      else {
        this.#popover.append(this.#dialog);
        this.#popover.hidden = false;
        this.#button.setAttribute("aria-expanded", "true");
        this.#place();
        this.#dialog.focus();
      }
    };
    this.#description.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && !this.#popover.hidden) {
        event.stopPropagation();
        this.#close();
        this.#button.focus();
      }
    });
    this.#description.addEventListener("focusout", (event) => {
      if (event.relatedTarget instanceof Node && !this.#description.contains(event.relatedTarget))
        this.#close();
    });
  }

  get data(): PageHeaderData {
    return this.#data;
  }

  set data(value: PageHeaderData) {
    this.#data = value;
    this.#headingText.data = value.title;
    this.#eyebrowText.data = value.eyebrow ?? "";
    this.#badgeText.data = value.badge ? " " + value.badge : "";
    this.#descriptionText.data = value.description ?? "";
    this.#fullText.data = value.description ?? "";
    for (const element of [this.#button, this.#dialog])
      element.setAttribute("aria-label", `About ${value.title}`);
    this.#aboutLink.replaceChildren();
    if (value.about) this.#aboutLink.append(this.#link(value.about));
    this.#drawBar();
    this.#measure();
  }

  connectedCallback(): void {
    this.classList.add("page-heading");
    this.replaceChildren(this.areas.eyebrow, this.#row, this.areas.actions);
    const view = this.ownerDocument.defaultView;
    this.#media = typeof view?.matchMedia === "function" ? view.matchMedia(PHONE) : undefined;
    this.#media?.addEventListener("change", this.#moveActions);
    this.#drawBar();
    this.#measure();
    if (typeof ResizeObserver !== "undefined") {
      this.#resize = new ResizeObserver(this.#measure);
      this.#resize.observe(this.areas.description);
    }
    this.#mutations = new MutationObserver(this.#measure);
    for (const area of [this.areas.description, this.areas.about]) {
      this.#mutations.observe(area, { childList: true, characterData: true, subtree: true });
    }
    this.ownerDocument.addEventListener("click", this.#outside);
    this.ownerDocument.defaultView?.addEventListener("resize", this.#place);
  }

  disconnectedCallback(): void {
    this.#resize?.disconnect();
    this.#mutations?.disconnect();
    this.#media?.removeEventListener("change", this.#moveActions);
    this.ownerDocument.removeEventListener("click", this.#outside);
    this.ownerDocument.defaultView?.removeEventListener("resize", this.#place);
    this.#close();
  }

  #link(data: HeaderLink): HTMLAnchorElement {
    const link = navigationLink(this, data.href);
    link.textContent = data.label;
    return link;
  }

  #close(): void {
    this.#dialog.remove();
    this.#popover.hidden = true;
    this.#button.setAttribute("aria-expanded", "false");
  }

  #outside = (event: MouseEvent): void => {
    if (
      !this.#popover.hidden &&
      event.target instanceof Node &&
      !this.#description.contains(event.target)
    )
      this.#close();
  };

  #place = (): void => {
    if (this.#popover.hidden) return;
    const width = this.#popover.getBoundingClientRect().width;
    const room = this.ownerDocument.documentElement.clientWidth - 16 - width;
    const left = Math.max(16, Math.min(this.#button.getBoundingClientRect().left, room));
    this.#popover.style.left = `${String(left - this.#description.getBoundingClientRect().left)}px`;
  };

  #measure = (): void => {
    const line = this.areas.description;
    const about = this.#data.about !== undefined || this.areas.about.hasChildNodes();
    this.#description.hidden = !(line.textContent || about);
    this.#button.hidden = !(about || line.scrollWidth > line.clientWidth + 1);
    if (this.#button.hidden) this.#close();
  };

  #drawBar(): void {
    this.#bar.replaceChildren();
    this.#actionBar.replaceChildren();
    const { ancestors, actions, title } = this.#data;
    if (!ancestors && !actions?.length) {
      this.#bar.remove();
      this.#actionBar.remove();
      return;
    }
    if (ancestors) {
      const nav = part("nav", "crumbs page-top-crumbs");
      nav.setAttribute("aria-label", "Breadcrumb");
      const list = part("ol", "");
      for (const parent of ancestors) {
        const item = part("li", "");
        item.append(this.#link(parent));
        list.append(item);
      }
      const current = part("li", "", title);
      current.setAttribute("aria-current", "page");
      list.append(current);
      nav.append(list);
      this.#bar.append(nav);
    }
    for (const action of actions ?? []) {
      const button = part("button", "button", action.label);
      button.type = "button";
      button.onclick = () => {
        this.dispatchEvent(
          new CustomEvent<HeaderActionDetail>("iki-action", {
            detail: { id: action.id },
            bubbles: true,
            composed: true,
          }),
        );
      };
      this.#actionBar.append(button);
    }
    if (this.isConnected) this.prepend(this.#bar);
    this.#moveActions();
  }

  #moveActions = (): void => {
    if (!this.#actionBar.hasChildNodes()) return;
    (this.#media?.matches ? this.areas.actions : this.#bar).append(this.#actionBar);
  };
}

if (!customElements.get("iki-page-header")) customElements.define("iki-page-header", IkiPageHeader);

declare global {
  interface HTMLElementTagNameMap {
    "iki-page-header": IkiPageHeader;
  }
}
