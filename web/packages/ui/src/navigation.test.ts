import { afterEach, describe, expect, it, vi } from "vitest";

import "./page-header.js";
import "./sidebar.js";

afterEach(() => {
  document.body.replaceChildren();
  vi.unstubAllGlobals();
});

describe("the page header", () => {
  it("keeps breadcrumbs, actions and the About link through later data and reordering", () => {
    const header = document.createElement("iki-page-header");
    const input = document.createElement("input");
    document.body.append(header, input);
    header.data = {
      title: "Review",
      ancestors: [
        { href: "/", label: "Home" },
        { href: "/projects", label: "Projects" },
      ],
      actions: [
        { id: "edit", label: "Edit" },
        { id: "export", label: "Export" },
      ],
      about: { href: "/guide", label: "Guide" },
    };
    const crumb = header.querySelector<HTMLAnchorElement>('[href="/projects"]');
    const action = header.querySelector<HTMLButtonElement>(".page-actions button:last-child");
    action?.focus();
    input.focus();
    header.data = {
      ...header.data,
      ancestors: [
        { href: "/projects", label: "Workspaces" },
        { href: "/", label: "Home" },
      ],
      actions: [
        { id: "export", label: "Download" },
        { id: "edit", label: "Edit" },
      ],
    };
    expect(document.activeElement).toBe(input);
    expect(header.querySelector(".page-actions button")).toBe(action);
    expect(action?.textContent).toBe("Download");
    action?.focus();
    expect(document.activeElement).toBe(action);
    expect(header.querySelector("nav a")).toBe(crumb);
    expect(crumb?.textContent).toBe("Workspaces");
    crumb?.focus();
    header.data = { ...header.data, ancestors: [...(header.data.ancestors ?? [])].reverse() };
    expect(document.activeElement).toBe(crumb);

    const routed = vi.fn((event: Event) => {
      event.preventDefault();
    });
    header.addEventListener("iki-navigate", routed);
    crumb?.click();
    expect((routed.mock.calls[0]?.[0] as CustomEvent).detail).toEqual({ href: "/projects" });
    const acted = vi.fn();
    header.addEventListener("iki-action", acted);
    action?.click();
    expect(acted).toHaveBeenCalledOnce();
    expect((acted.mock.calls[0]?.[0] as CustomEvent).detail).toEqual({ id: "export" });

    const toggle = header.querySelector<HTMLButtonElement>(".page-about");
    toggle?.click();
    const about = header.querySelector<HTMLAnchorElement>('[role="dialog"] a');
    about?.focus();
    header.data = {
      ...header.data,
      title: "New review",
      about: { href: "/guide", label: "Handbook" },
    };
    expect(header.querySelector('[role="dialog"] a')).toBe(about);
    expect(about?.textContent).toBe("Handbook");
    expect(document.activeElement).toBe(about);
    about?.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, key: "Escape" }));
    expect(document.activeElement).toBe(toggle);
  });

  it("keeps a focused action when the media query moves it below the heading and back", () => {
    const media = Object.assign(new EventTarget(), { matches: false });
    vi.stubGlobal("matchMedia", () => media);
    const header = document.createElement("iki-page-header");
    document.body.append(header);
    header.data = { title: "Review", actions: [{ id: "share", label: "Share" }] };
    const action = header.querySelector<HTMLButtonElement>(".page-top button");
    action?.focus();
    media.matches = true;
    media.dispatchEvent(new Event("change"));
    expect(header.areas.actions.querySelector("button")).toBe(action);
    expect(document.activeElement).toBe(action);

    header.data = { ...header.data, description: "Team review" };
    expect(document.activeElement).toBe(action);
    media.matches = false;
    media.dispatchEvent(new Event("change"));
    expect(header.querySelector(".page-top button")).toBe(action);
    expect(document.activeElement).toBe(action);
    header.data = { title: "Complete" };
    expect(action?.isConnected).toBe(false);
    expect(header.querySelector(".page-top")).toBeNull();
  });

  it("shows its description, restores focus on Escape and reports actions", () => {
    const header = document.createElement("iki-page-header");
    header.data = {
      title: "Activity",
      description: "Review history",
      about: { label: "Guide", href: "/guide" },
      actions: [{ id: "export", label: "Export" }],
      ancestors: [{ href: "/", label: "Home" }],
    };
    document.body.append(header);
    expect(header.heading.textContent).toBe("Activity");
    expect(header.querySelector("nav [aria-current]")?.textContent).toBe("Activity");
    const button = header.querySelector<HTMLButtonElement>(".page-about");
    button?.click();
    expect(document.activeElement?.getAttribute("role")).toBe("dialog");
    document.activeElement?.dispatchEvent(
      new KeyboardEvent("keydown", { key: "Escape", bubbles: true }),
    );
    expect(document.activeElement).toBe(button);
    expect(header.querySelector('[role="dialog"]')).toBeNull();
    const act = vi.fn();
    document.body.addEventListener("iki-action", act, { once: true });
    header.querySelector<HTMLButtonElement>(".page-actions button")?.click();
    expect(act).toHaveBeenCalledOnce();
    expect((act.mock.calls[0]?.[0] as CustomEvent<{ id: string }>).detail.id).toBe("export");
  });
});

describe("the sidebar", () => {
  it("keeps the opener of a dialog when a query changes the sidebar behind it", () => {
    const sidebar = document.createElement("iki-sidebar");
    const input = document.createElement("input");
    document.body.append(sidebar, input);
    const projects = { href: "/projects/", label: "Projects" };
    const section = { label: "Projects", links: [projects] };
    sidebar.data = { label: "Main", sections: [section] };
    const opener = sidebar.querySelector("a");
    opener?.focus();
    input.focus();

    sidebar.data = {
      label: "Main",
      sections: [
        { label: "Saved", links: [{ href: "/views/", label: "My views" }] },
        {
          ...section,
          links: [
            { href: "/projects/atlas/", label: "Atlas" },
            { ...projects, current: true, count: 2 },
          ],
        },
      ],
    };

    expect(document.activeElement).toBe(input);
    expect(sidebar.querySelector('[href="/projects/"]')).toBe(opener);
    expect(opener?.getAttribute("aria-current")).toBe("page");
    expect(opener?.textContent).toBe("Projects 2");
    opener?.focus();
    expect(document.activeElement).toBe(opener);
  });

  it("keeps each action and each repeated link target with its own section when groups reorder", () => {
    const sidebar = document.createElement("iki-sidebar");
    document.body.append(sidebar);
    const link = { label: "View", href: "/view" };
    const a = { label: "A", action: { ...link, label: "Open view" }, links: [link] };
    const b = { label: "B", links: [link] };
    sidebar.data = { label: "Main", sections: [a, b] };
    const action = sidebar.querySelector<HTMLAnchorElement>(".nav-add");
    const first = sidebar.querySelector('[aria-label="A"] .nav-item');
    const second = sidebar.querySelector('[aria-label="B"] .nav-item');
    action?.focus();
    sidebar.data = {
      label: "Main",
      sections: [b, { ...a, action: { ...link, label: "Open saved view" } }],
    };
    expect(sidebar.querySelector("a")).toBe(second);
    expect(sidebar.querySelector('[aria-label="A"] .nav-item')).toBe(first);
    expect(sidebar.querySelector(".nav-add")).toBe(action);
    expect(action?.title).toBe("Open saved view");
    expect(document.activeElement).toBe(action);
  });

  it("expands a section, keeps its focused item and lets the host route a plain click", () => {
    const sidebar = document.createElement("iki-sidebar");
    const group = {
      label: "Docs",
      disclosure: { id: "guides", open: false },
      links: [{ href: "/guide", label: "Guide", current: true }],
    };
    sidebar.data = { label: "Library", sections: [group] };
    document.body.append(sidebar);
    const button = sidebar.querySelector("button");
    button?.focus();
    const toggle = vi.fn();
    sidebar.addEventListener("iki-toggle", toggle);
    button?.click();
    expect(sidebar.querySelector("#guides")?.hasAttribute("hidden")).toBe(false);
    expect((toggle.mock.calls[0]?.[0] as CustomEvent).detail).toEqual({ id: "guides", open: true });
    sidebar.data = {
      ...sidebar.data,
      sections: [{ ...group, disclosure: { id: "guides", open: true } }],
    };
    expect(document.activeElement).toBe(button);
    button?.click();
    expect(sidebar.querySelector("#guides")?.hasAttribute("hidden")).toBe(true);
    expect((toggle.mock.calls[1]?.[0] as CustomEvent).detail).toEqual({
      id: "guides",
      open: false,
    });
    const navigate = vi.fn((event: Event) => {
      event.preventDefault();
    });
    sidebar.addEventListener("iki-navigate", navigate);
    expect(
      sidebar
        .querySelector("a")
        ?.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true })),
    ).toBe(false);
    expect(navigate).toHaveBeenCalledOnce();
    sidebar.data = {
      label: "Library",
      sections: [{ links: [{ label: "Invalid URL", href: "data:text/plain,example" }] }],
    };
    expect(sidebar.querySelector("a")?.hasAttribute("href")).toBe(false);
  });
});
