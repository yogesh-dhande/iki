import { afterEach, expect, it } from "vitest";

import "./sidebar.js";

afterEach(() => {
  document.body.replaceChildren();
});

function present<T>(node: T | null | undefined): T {
  if (node == null) throw new Error("Missing sidebar content");
  return node;
}

it("keeps a pressed path, its label and its disclosure arrow during sidebar updates", () => {
  const sidebar = document.createElement("iki-sidebar");
  document.body.append(sidebar);
  const action = { href: "/projects/new/", label: "New project", icon: ["M3 12h18"] };
  const section = { label: "Projects", links: [], action };
  const disclosure = { id: "types", open: false, icon: ["M2 3v4"] };
  sidebar.data = {
    label: "Main",
    sections: [section, { label: "Types", links: [], disclosure }],
  };
  const link = present(sidebar.querySelector("a"));
  const path = present(link.querySelector("path"));
  const label = present(link.querySelector("span")?.firstChild);
  const button = present(sidebar.querySelector("button"));
  const arrow = present(button.lastElementChild?.firstElementChild);
  const buttonLabel = present(button.querySelector("span")?.firstChild);
  const symbol = present(button.firstElementChild?.firstElementChild);
  path.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));

  sidebar.data = {
    label: "Main",
    sections: [
      {
        ...section,
        action: { ...action, label: "Create project", icon: ["M3 12h18", "M12 3v18"] },
      },
      {
        label: "Record types",
        links: [],
        disclosure: { ...disclosure, open: true, icon: ["M1 1h9"] },
      },
    ],
  };
  expect(link.querySelector("path")).toBe(path);
  expect(link.querySelector("svg")).toBe(path.parentElement);
  expect(link.querySelector("span")?.firstChild).toBe(label);
  expect(label.textContent).toBe("Create project");
  expect(link.querySelectorAll("path")).toHaveLength(2);
  expect(button.firstElementChild?.firstElementChild).toBe(symbol);
  expect(symbol.getAttribute("d")).toBe("M1 1h9");
  expect(button.lastElementChild?.firstElementChild).toBe(arrow);
  expect(button.querySelector("span")?.firstChild).toBe(buttonLabel);
  expect(buttonLabel.textContent).toBe("Record types");
  expect(button.getAttribute("aria-expanded")).toBe("true");

  sidebar.data = {
    label: "Main",
    sections: [section, { label: "Types", links: [], disclosure: { id: "types", open: false } }],
  };
  expect(link.querySelectorAll("path")).toHaveLength(1);
  expect(link.querySelector("path")).toBe(path);
  expect(button.querySelectorAll("svg")).toHaveLength(1);
  expect(button.lastElementChild?.firstElementChild).toBe(arrow);
});

it("updates marks, qualifiers, counts and badges without replacing the visible link text", () => {
  const sidebar = document.createElement("iki-sidebar");
  document.body.append(sidebar);
  const data = {
    href: "/records/",
    label: "Cells",
    qualifier: "Cell bank",
    count: 1,
    mark: { shape: "dot" as const, swatch: 1 },
    badge: { text: "2", description: "unread" },
  };
  sidebar.data = { label: "Main", sections: [{ links: [data] }] };
  const link = present(sidebar.querySelector("a"));
  const label = present(link.querySelector(".nav-label")?.firstChild);
  const mark = present(link.querySelector("[data-swatch]"));
  const qualifier = present(link.querySelector(".type-plugin")?.firstChild);
  const count = present(link.querySelector(".nav-count")?.firstChild);
  const badge = present(link.querySelector(".nav-badge")?.firstChild);

  sidebar.data = {
    label: "Main",
    sections: [
      {
        links: [
          {
            ...data,
            label: "Stocks",
            qualifier: "Bank",
            count: 7,
            mark: { shape: "square", swatch: 3 },
            badge: { text: "9", description: "new" },
          },
        ],
      },
    ],
  };
  expect(link.querySelector(".nav-label")?.firstChild).toBe(label);
  expect(link.querySelector(".type-plugin")?.firstChild).toBe(qualifier);
  expect(link.querySelector(".nav-count")?.firstChild).toBe(count);
  expect(link.querySelector(".nav-badge")?.firstChild).toBe(badge);
  expect(link.querySelector(".swatch")).toBe(mark);
  expect(mark.getAttribute("data-swatch")).toBe("3");
  expect(link.textContent).toBe("Stocks Bank 7 9 new");
  sidebar.data = { label: "Main", sections: [{ links: [{ href: data.href, label: "Cells" }] }] };
  expect(link.textContent).toBe("Cells");
  expect(link.querySelector(".nav-label")?.firstChild).toBe(label);
});

it("keeps keyboard focus on the expanded disclosure and on a link inside it during refresh", () => {
  const sidebar = document.createElement("iki-sidebar");
  document.body.append(sidebar);
  const entry = { label: "All records", href: "/records/", icon: ["M2 2h20"] };
  const section = { label: "Records", disclosure: { id: "records", open: true }, links: [entry] };
  sidebar.data = { label: "Main", sections: [section] };
  const button = present(sidebar.querySelector("button"));
  const records = present(sidebar.querySelector("a"));
  const iconPath = records.querySelector("path");
  button.focus();
  sidebar.data = { label: "Navigation", sections: [section] };
  expect(document.activeElement).toBe(button);
  expect(button.getAttribute("aria-expanded")).toBe("true");
  records.focus();
  sidebar.data = {
    label: "Main",
    sections: [
      {
        ...section,
        links: [
          { label: "People", href: "/people/" },
          { ...entry, count: 2, icon: ["M3 2h20"] },
        ],
      },
    ],
  };
  expect(document.activeElement).toBe(records);
  expect(sidebar.querySelector("#records")?.hasAttribute("hidden")).toBe(false);
  expect(records.querySelector("path")).toBe(iconPath);
  expect(iconPath?.getAttribute("d")).toBe("M3 2h20");
  expect(records.textContent).toBe("All records 2");
});
