import { afterEach, describe, expect, it, vi } from "vitest";

import "./page-header.js";
import "./sidebar.js";

afterEach(() => {
  document.body.replaceChildren();
});

describe("the page header", () => {
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
    expect(document.activeElement?.getAttribute("aria-controls")).toBe("guides");
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
