import {
  arrange,
  icon,
  NavigationItems,
  navigationLink,
  part,
  place,
  setText,
} from "./navigation.js";

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
  readonly #items = new NavigationItems();
  readonly #crumbs = part("nav", "crumbs page-top-crumbs");
  readonly #trail = part("ol", "");
  readonly #current = part("li", "");
  readonly #actionBar = part("div", "page-actions");
  #data: PageHeaderData = { title: "" };
  #resize?: ResizeObserver;
  #mutations?: MutationObserver;
  #media: MediaQueryList | undefined;

  constructor() {
    super();
    this.#crumbs.setAttribute("aria-label", "Breadcrumb");
    this.#crumbs.append(this.#trail);
    this.#current.setAttribute("aria-current", "page");
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
    const { about, ancestors, actions, title } = this.#data;
    const retain = this.#items.next();
    if (about) {
      const link = retain(["about", about.href], () => navigationLink(this, about.href));
      setText(link, about.label);
      arrange(this.#aboutLink, [link]);
    } else arrange(this.#aboutLink, []);
    if (ancestors) {
      const trail = ancestors.map((ancestor) => {
        const { item, link } = retain(["ancestor", ancestor.href], () => {
          const item = part("li", "");
          const link = navigationLink(this, ancestor.href);
          item.append(link);
          return { item, link };
        });
        setText(link, ancestor.label);
        return item;
      });
      setText(this.#current, title);
      arrange(this.#trail, [...trail, this.#current]);
      if (this.#crumbs.parentNode !== this.#bar) this.#bar.prepend(this.#crumbs);
    } else this.#crumbs.remove();
    const buttons = (actions ?? []).map(({ id, label }) => {
      const button = retain(["action", id], () => {
        const control = part("button", "button");
        control.type = "button";
        control.onclick = () => {
          this.dispatchEvent(
            new CustomEvent<HeaderActionDetail>("iki-action", {
              detail: { id },
              bubbles: true,
              composed: true,
            }),
          );
        };
        return control;
      });
      setText(button, label);
      return button;
    });
    arrange(this.#actionBar, buttons);
    if (!buttons.length) this.#actionBar.remove();
    if (!ancestors && !buttons.length) this.#bar.remove();
    else if (this.isConnected && this.#bar.parentNode !== this) this.prepend(this.#bar);
    this.#moveActions();
  }

  #moveActions = (): void => {
    if (!this.#actionBar.hasChildNodes()) return;
    const parent = this.#media?.matches ? this.areas.actions : this.#bar;
    if (this.#actionBar.parentNode !== parent) place(parent, this.#actionBar);
  };
}

if (!customElements.get("iki-page-header")) customElements.define("iki-page-header", IkiPageHeader);

declare global {
  interface HTMLElementTagNameMap {
    "iki-page-header": IkiPageHeader;
  }
}
