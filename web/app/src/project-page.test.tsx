/**
 * A project's page (ADR 0176): its heading, its tabs with their counts, Automation's three,
 * the New menu, and the Overview's cards, against a fake API.
 */

import { QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, RouterProvider } from "@tanstack/react-router";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import type { Schemas } from "@ikisci/sdk";

import { queryClient } from "./api";
import { initials, SWATCHES, swatchColor, swatchOf } from "./project-color";
import { createAppRouter } from "./router";
import { answer, calls, ME, page, project, signedIn, TEAM } from "./test-server";

const PROJECT = project({ description: "Kinases\nand their inhibitors" });
const BASE = `/api/v1/projects/${PROJECT.id}`;

async function renderApp(path: string) {
  const router = createAppRouter(createMemoryHistory({ initialEntries: [path] }));
  render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  await screen.findByRole("heading", { level: 1 });
  return { router, user: userEvent.setup() };
}

const ADA = {
  id: "00000000-0000-7000-8000-0000000000u2",
  email: "ada@example.com",
  name: "Ada Lovelace",
  kind: "person" as const,
  active: true,
  imported_from: "",
};

function member(
  id: string,
  user: Schemas["UserOut"] | null,
  team = false,
): Schemas["ProjectMemberOut"] {
  return {
    id,
    role: "editor",
    user,
    team: team ? TEAM : null,
    team_size: team ? 4 : null,
    added_at: "2026-03-14T09:00:00Z",
    lab_roles: [],
  };
}

function change(overrides: Partial<Schemas["ProjectChangeOut"]>): Schemas["ProjectChangeOut"] {
  return {
    number: 2,
    kind: "edited",
    entry: false,
    changed_fields: ["name"],
    user: { ...ADA },
    via: "web",
    client: "",
    agent: null,
    run: null,
    run_withheld: false,
    reason: "",
    state: null,
    holds: [],
    submitted: null,
    created_at: new Date(Date.now() - 2 * 3600_000).toISOString(),
    entity: {
      id: "00000000-0000-7000-8000-0000000000e1",
      hrid: "PLS-000001",
      title: "pUC19c",
      type: "iki/dna.Plasmid",
      archived: false,
    },
    ...overrides,
  };
}

function onProject(overrides: Partial<Schemas["ProjectOut"]> = {}) {
  const found = { ...PROJECT, ...overrides };
  answer("GET /api/v1/projects", page([found]));
  answer(
    `GET ${BASE}/members`,
    page([member("m1", { ...ME }), member("m2", ADA), member("m3", null, true)]),
  );
  return found;
}

describe("a project's colour", () => {
  it("is one of the swatches, the same for the same ID", () => {
    const ids = Array.from(
      { length: 64 },
      (_, i) => `00000000-0000-7000-8000-${String(i).padStart(12, "0")}`,
    );
    const swatches = ids.map(swatchOf);
    expect(swatches.every((s) => s >= 1 && s <= SWATCHES)).toBe(true);
    expect(ids.map(swatchOf)).toEqual(swatches);
    // Spread over them, not one for all.
    expect(new Set(swatches).size).toBe(SWATCHES);
    expect(swatchColor(ids[0] ?? "")).toBe(`var(--swatch-${String(swatches[0])})`);
  });

  it("goes with people's initials", () => {
    expect(initials("Ada Lovelace")).toBe("AL");
    expect(initials("Rosalind Elsie Franklin")).toBe("RF");
    expect(initials("bob")).toBe("B");
    expect(initials("maurice.wilkins@example.com")).toBe("MW");
  });
});

describe("a project's page", () => {
  it("says where it is, what it's called and who's in it", async () => {
    signedIn();
    onProject();
    await renderApp("/projects/kinase-atlas/");
    const crumbs = screen.getByRole("navigation", { name: "Breadcrumb" });
    expect(within(crumbs).getByRole("link", { name: "Projects" })).toHaveAttribute(
      "href",
      "/projects/",
    );
    expect(within(crumbs).getByRole("link", { name: "Kinase Atlas" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    // Its first line; the rest is under About.
    expect(
      await screen.findByText("Kinases · 3 members · you’re an Admin ·", { exact: false }),
    ).toBeVisible();
    const tabs = screen.getByRole("navigation", { name: "Project" });
    expect(within(tabs).getByRole("link", { name: "Members 3" })).toBeVisible();
    expect(
      within(tabs)
        .getAllByRole("link")
        .map((a) => a.textContent),
    ).toEqual(["Overview", "Entities", "Runs", "Automation", "Members 3", "Settings"]);
    const members = screen.getByRole("list", { name: "Members" });
    expect(
      within(members)
        .getAllByRole("listitem")
        .map((li) => li.textContent),
    ).toEqual(["RFRosalind Franklin", "ALAda Lovelace", "SBStructural Biology"]);
    expect(screen.getByRole("link", { name: "Manage" })).toHaveAttribute(
      "href",
      "/projects/kinase-atlas/members/",
    );
  });

  it("counts its entities by type, each opening its table held to the project", async () => {
    signedIn();
    onProject();
    answer("GET /api/v1/entities/counts", {
      types: [
        { type: "iki/dna.Plasmid", label: "Plasmid", count: 2 },
        { type: "iki/dna.Primer", label: "Primer", count: 1250 },
      ],
      total: 1252,
    });
    await renderApp("/projects/kinase-atlas/");
    const counts = await screen.findByRole("list", { name: "Entities by type" });
    expect(within(counts).getByRole("link", { name: "Primer 1,250" })).toHaveAttribute(
      "href",
      `/entities/types/iki/dna.Primer?project=${PROJECT.id}`,
    );
    expect(
      screen.getByRole("link", { name: "Open in Entities, filtered to this project" }),
    ).toHaveAttribute("href", `/entities/?project=${PROJECT.id}`);
    const tabs = screen.getByRole("navigation", { name: "Project" });
    expect(within(tabs).getByRole("link", { name: "Entities 1252" })).toBeVisible();
    expect(calls("GET", "/api/v1/entities/counts")[0]?.query.get("project")).toBe(PROJECT.id);
  });

  it("says which types it didn't count, where counting would be slow", async () => {
    signedIn();
    onProject();
    answer("GET /api/v1/entities/counts", {
      types: [{ type: "iki/dna.Plasmid", label: "Plasmid", count: null }],
      total: null,
    });
    await renderApp("/projects/kinase-atlas/");
    const counts = await screen.findByRole("list", { name: "Entities by type" });
    expect(within(counts).getByRole("link", { name: "Plasmid not counted" })).toBeVisible();
    const tabs = screen.getByRole("navigation", { name: "Project" });
    expect(within(tabs).getByRole("link", { name: "Entities" })).toBeVisible();
  });

  it("leaves the counts out where the server doesn't count", async () => {
    signedIn();
    onProject();
    await renderApp("/projects/kinase-atlas/");
    await waitFor(() => {
      expect(calls("GET", "/api/v1/entities/counts")).toHaveLength(1);
    });
    expect(screen.queryByRole("heading", { name: "Entities in this project" })).toBeNull();
    const tabs = screen.getByRole("navigation", { name: "Project" });
    expect(within(tabs).getByRole("link", { name: "Entities" })).toBeVisible();
  });

  it("lists what changed lately, and Auditors open the audit log from it", async () => {
    signedIn({ can: { ...ME.can, read_audit: true } });
    onProject();
    answer(`GET ${BASE}/changes`, [
      change({
        number: 3,
        kind: "state_changed",
        changed_fields: ["status"],
        state: {
          field: { name: "status", label: "Status" },
          from: { name: "draft", label: "Draft" },
          to: { name: "registered", label: "Registered" },
          signatures: [{ id: "s1", manifestation: "Approved", present: true }],
        },
      }),
      change({ reason: "Rename" }),
      change({ number: 1, kind: "created", user: null, via: "system" }),
    ]);
    await renderApp("/projects/kinase-atlas/");
    const list = await screen.findByRole("list", { name: "Recent changes" });
    expect(
      within(list)
        .getAllByRole("listitem")
        .map((li) => li.textContent),
    ).toEqual([
      "ALAda Lovelace set Status of PLS-000001 pUC19c to Registered, signed2 hours ago",
      "ALAda Lovelace edited PLS-000001 pUC19c: name · “Rename”2 hours ago",
      "ikiIki created PLS-000001 pUC19c2 hours ago",
    ]);
    expect(within(list).getAllByRole("link", { name: "PLS-000001" })[0]).toHaveAttribute(
      "href",
      "/entities/PLS-000001/?project=kinase-atlas",
    );
    expect(screen.getByRole("link", { name: "Project audit log" })).toHaveAttribute(
      "href",
      `/audit/?project=${PROJECT.id}`,
    );
    expect(calls("GET", `${BASE}/changes`)[0]?.query.get("limit")).toBe("8");
  });

  it("sums up its automation, and only its Admins hear of webhooks", async () => {
    signedIn();
    onProject();
    answer("GET /api/v1/workflows", [{ name: "a" }, { name: "b" }]);
    answer(`GET ${BASE}/triggers`, [
      { name: "t1", enabled: true },
      { name: "t2", enabled: false },
    ]);
    answer(`GET ${BASE}/webhooks`, page([{ id: "w1" }]));
    await renderApp("/projects/kinase-atlas/");
    const card = screen.getByRole("region", { name: "Automation" });
    expect(
      await within(card).findByText("2 workflows · 1 of 2 triggers on · 1 webhook"),
    ).toBeVisible();
    expect(
      within(card).getByRole("link", { name: "Workflows, triggers and webhooks" }),
    ).toHaveAttribute("href", "/projects/kinase-atlas/workflows/");
  });
});

describe("a project's Automation tab", () => {
  it("holds its workflows, triggers and webhooks, each at its own address", async () => {
    signedIn();
    onProject();
    answer(`GET ${BASE}/triggers`, []);
    await renderApp("/projects/kinase-atlas/triggers/");
    const tabs = screen.getByRole("navigation", { name: "Project" });
    expect(within(tabs).getByRole("link", { name: "Automation" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    const automation = screen.getByRole("navigation", { name: "Automation" });
    expect(
      within(automation)
        .getAllByRole("link")
        .map((a) => [a.textContent, a.getAttribute("aria-current")]),
    ).toEqual([
      ["Workflows", null],
      ["Triggers", "page"],
      ["Webhooks", null],
    ]);
  });

  it("isn't a row of its own elsewhere", async () => {
    signedIn();
    onProject();
    await renderApp("/projects/kinase-atlas/");
    expect(screen.queryByRole("navigation", { name: "Automation" })).toBeNull();
    const tabs = screen.getByRole("navigation", { name: "Project" });
    expect(within(tabs).getByRole("link", { name: "Automation" })).not.toHaveAttribute(
      "aria-current",
    );
  });

  it("opens on workflows from its own address, or webhooks for an Admin who can't read it", async () => {
    signedIn();
    onProject();
    const { router } = await renderApp("/projects/kinase-atlas/automation/");
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/projects/kinase-atlas/workflows/");
    });

    cleanup();
    queryClient.clear();
    onProject({
      my_role: null,
      can: { ...PROJECT.can, read: false, edit: false, manage: false, run: [] },
    });
    const again = await renderApp("/projects/kinase-atlas/automation/");
    await waitFor(() => {
      expect(again.router.state.location.pathname).toBe("/projects/kinase-atlas/webhooks/");
    });
  });
});

describe("a project's New menu", () => {
  it("makes a record, a run or a workflow run there", async () => {
    signedIn();
    onProject();
    const { router, user } = await renderApp("/projects/kinase-atlas/");
    await user.click(screen.getByRole("button", { name: "New in Kinase Atlas" }));
    const menu = await screen.findByRole("menu", { name: "New in Kinase Atlas" });
    expect(
      within(menu)
        .getAllByRole("menuitem")
        .map((m) => m.textContent),
    ).toEqual(["Entity", "Run", "Workflow run"]);
    await user.click(within(menu).getByRole("menuitem", { name: "Run" }));
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/projects/kinase-atlas/runs/new/");
    });
  });

  it("isn't there for someone who can't make anything in it", async () => {
    signedIn();
    onProject({
      my_role: "viewer",
      can: { ...PROJECT.can, edit: false, manage: false, manage_members: false, run: [] },
    });
    await renderApp("/projects/kinase-atlas/");
    expect(await screen.findByText(/you’re a Viewer/)).toBeVisible();
    expect(screen.queryByRole("button", { name: /^New/ })).toBeNull();
    expect(screen.getByRole("link", { name: "All members" })).toBeVisible();
  });
});
