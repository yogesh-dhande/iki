/** The admin area's frame (ADR 0176): its sidebar, breadcrumb and account, per person. */

import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ADMIN_GROUPS, initials } from "./admin";
import { renderApp } from "./entity-fixtures";
import { answer, ME, signedIn } from "./test-server";

const NONE = ME.can;
const EVERYTHING = {
  manage_org: true,
  read_audit: true,
  create_projects: true,
  manage_webhooks: true,
  read_costs: true,
  manage_compute: true,
};

/** Compute's overview, with nothing going on: a page of the area to open. */
function onCompute() {
  answer("GET /api/v1/compute/overview", {
    queues: [],
    workers: [],
    machines: [],
    as_of: new Date().toISOString(),
  });
}

/** Each group the sidebar shows, with its links' names and addresses. */
function sidebar(): [string, [string, string | null][]][] {
  const nav = screen.getByRole("navigation", { name: "Admin" });
  return within(nav)
    .getAllByRole("list")
    .map((list) => [
      list.getAttribute("aria-labelledby")
        ? (document.getElementById(list.getAttribute("aria-labelledby") ?? "")?.textContent ?? "")
        : "",
      within(list)
        .getAllByRole("link")
        .map((a) => [a.textContent, a.getAttribute("href")]),
    ]);
}

describe("the admin area", () => {
  it("offers someone who may open everything each page, in the ADR's four groups", async () => {
    signedIn({ can: EVERYTHING });
    onCompute();
    await renderApp("/compute/");
    expect(sidebar()).toEqual([
      [
        "Organization",
        [
          ["Sign-in and provisioning", "/settings/sign-in/"],
          ["People", "/people/"],
          ["Apps", "/settings/apps/"],
          ["Email", "/settings/email/"],
        ],
      ],
      [
        "Data",
        [
          ["Registries", "/settings/registries/"],
          ["Plugins", "/settings/plugins/"],
          ["Outside APIs", "/settings/outside-apis/"],
          ["Webhooks", "/settings/webhooks/"],
        ],
      ],
      [
        "Compute and cost",
        [
          ["Compute", "/compute/"],
          ["Costs and budgets", "/costs/"],
        ],
      ],
      [
        "Compliance",
        [
          ["Audit log", "/audit/"],
          ["Electronic signatures", "/settings/signatures/"],
          ["Legal holds", "/settings/legal-holds/"],
        ],
      ],
    ]);
  });

  it("shows an auditor only what they may open, and no group left empty", async () => {
    signedIn({ can: { ...NONE, read_audit: true, read_costs: true } });
    await renderApp("/compute/");
    expect(sidebar()).toEqual([
      ["Compute and cost", [["Costs and budgets", "/costs/"]]],
      [
        "Compliance",
        [
          ["Audit log", "/audit/"],
          ["Electronic signatures", "/settings/signatures/"],
          ["Legal holds", "/settings/legal-holds/"],
        ],
      ],
    ]);
    // The page itself still checks: Compute isn't theirs.
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Not found");
  });

  it("leads back to Iki, says where you are, and has your account at its foot", async () => {
    signedIn({ can: { ...NONE, manage_org: true } });
    await renderApp("/compute/");
    const nav = screen.getByRole("navigation", { name: "Admin" });
    expect(within(nav).getByRole("link", { name: "Back to Iki" })).toHaveAttribute("href", "/");
    const account = within(nav).getByRole("link", { name: /Rosalind Franklin/ });
    expect(account).toHaveAttribute("href", "/account/");
    expect(account).toHaveTextContent("RFRosalind Franklin");
    const signOut = within(nav).getByRole("button", { name: "Sign out" });
    expect(signOut.closest("form")).toHaveAttribute("action", "/logout/");
    const crumbs = screen.getByRole("navigation", { name: "Breadcrumb" });
    expect(within(crumbs).getByRole("link", { name: "Admin" })).toHaveAttribute("href", "/admin/");
    // The main navigation isn't there: the sidebar is the area's own.
    expect(screen.queryByRole("navigation", { name: "Main" })).not.toBeInTheDocument();
  });

  it("folds its pages under a menu button, for narrow screens", async () => {
    signedIn({ can: EVERYTHING });
    onCompute();
    const { user } = await renderApp("/compute/");
    const menu = screen.getByRole("button", { name: "Menu" });
    expect(menu).toHaveAttribute("aria-expanded", "false");
    expect(menu).toHaveAttribute("aria-controls", "admin-menu");
    await user.click(menu);
    expect(menu).toHaveAttribute("aria-expanded", "true");
  });

  it("lists, for every item, what it needs, an icon and an address", () => {
    const can = new Set(Object.keys(EVERYTHING));
    for (const item of ADMIN_GROUPS.flatMap((g) => g.items)) {
      expect(item.requires.length).toBeGreaterThan(0);
      for (const need of item.requires) expect(can).toContain(need);
      expect(item.icon.length).toBeGreaterThan(0);
      expect(item.href).toMatch(/^\/.*\/$/);
    }
  });

  it("badges the account with its initials", () => {
    expect(initials("Rosalind Franklin")).toBe("RF");
    expect(initials("Rosalind Elsie Franklin")).toBe("RF");
    expect(initials("rosalind@example.com")).toBe("R");
    expect(initials("Ørsted")).toBe("Ø");
  });
});
