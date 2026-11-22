/**
 * The sidebar (ADR 0176) against a fake API: what it lists for whom, which item is
 * current, the Entities section's counts and how it stays as the reader left it, the
 * account menu, the drawer, and search everything. The browser tests check it end to end,
 * with axe, at a phone's width, on the server's pages and the app's.
 */

import { QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, RouterProvider } from "@tanstack/react-router";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Schemas } from "@ikisci/sdk";

import { queryClient } from "./api";
import { createAppRouter } from "./router";
import { adminLink, currentOf, ENTITIES_COOKIE, initials } from "./sidebar";
import { answer, calls, ME, page, project, respond, signedIn } from "./test-server";

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

function nav(): HTMLElement {
  return screen.getByRole("navigation", { name: "Main" });
}

function saved(overrides: Partial<Schemas["SavedSearchOut"]> = {}): Schemas["SavedSearchOut"] {
  return {
    id: "00000000-0000-7000-8000-0000000000s1",
    project: project().id,
    name: "Ampicillin plasmids",
    query: { q: "ampicillin" },
    shared: false,
    created_by: { id: ME.id, name: ME.name },
    created_at: "2026-11-15T09:00:00Z",
    updated_at: "2026-11-15T09:00:00Z",
    can: { change: true, delete: true },
    ...overrides,
  };
}

afterEach(() => {
  document.cookie = `${ENTITIES_COOKIE}=; path=/; max-age=0`;
});

describe("what it lists", () => {
  it("lists each type the reader can read, with how many, and links to its table", async () => {
    signedIn();
    answer("GET /api/v1/projects", page([]));
    answer("GET /api/v1/entities/counts", {
      types: [
        { type: "iki/dna.Plasmid", label: "Plasmid", count: 3 },
        { type: "iki/core.Primer", label: "Primer", count: 1200 },
      ],
      total: 1203,
    });
    await renderApp("/projects/");
    const entities = within(nav()).getByRole("button", { name: "Entities" });
    expect(entities).toHaveAttribute("aria-expanded", "true");
    const plasmid = await within(nav()).findByRole("link", { name: "Plasmid 3" });
    expect(plasmid).toHaveAttribute("href", "/entities/types/iki/dna.Plasmid/");
    expect(within(nav()).getByRole("link", { name: "Primer 1,200" })).toBeVisible();
    expect(within(nav()).getByRole("link", { name: "All entities 1,203" })).toHaveAttribute(
      "href",
      "/entities/",
    );
  });

  it("lists every type uncounted where counting would be slow", async () => {
    signedIn();
    answer("GET /api/v1/projects", page([]));
    answer("GET /api/v1/entities/counts", {
      types: [{ type: "iki/dna.Plasmid", label: "Plasmid", count: null }],
      total: null,
    });
    await renderApp("/projects/");
    const plasmid = await within(nav()).findByRole("link", { name: "Plasmid" });
    expect(plasmid).toHaveAttribute("href", "/entities/types/iki/dna.Plasmid/");
    expect(within(nav()).getByRole("link", { name: "All entities" })).toBeVisible();
  });

  it("lists All entities alone where the counts can't be read", async () => {
    signedIn();
    answer("GET /api/v1/projects", page([]));
    answer("GET /api/v1/entities/counts", respond(503));
    await renderApp("/projects/");
    await waitFor(() => {
      expect(calls("GET", "/api/v1/entities/counts")).toHaveLength(1);
    });
    const types = document.getElementById("nav-entities");
    expect(types).not.toBeNull();
    expect(
      within(types ?? document.body)
        .getAllByRole("link")
        .map((a) => a.textContent),
    ).toEqual(["All entities"]);
  });

  it("stays open or closed as the reader left it, on every page", async () => {
    signedIn();
    answer("GET /api/v1/projects", page([]));
    const { user } = await renderApp("/projects/");
    await user.click(within(nav()).getByRole("button", { name: "Entities" }));
    expect(within(nav()).getByRole("button", { name: "Entities" })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
    expect(within(nav()).queryByRole("link", { name: "All entities" })).toBeNull();
    // The server reads it too, to draw its pages the same way.
    expect(document.cookie).toContain(`${ENTITIES_COOKIE}=closed`);
  });

  it("lists their projects, marks the one they're on, and offers a new one", async () => {
    signedIn();
    const atlas = project();
    const calls_ = project({ id: "p2", name: "Variant Calls", slug: "variant-calls" });
    answer("GET /api/v1/projects", ({ query }: { query: URLSearchParams }) =>
      page(query.get("slug") ? [calls_] : [atlas, calls_]),
    );
    await renderApp("/projects/variant-calls/");
    const here = within(nav()).getByRole("link", { name: "Variant Calls" });
    expect(here).toHaveAttribute("aria-current", "page");
    expect(within(nav()).getByRole("link", { name: "All projects" })).not.toHaveAttribute(
      "aria-current",
    );
    expect(within(nav()).getByRole("link", { name: "New project" })).toHaveAttribute(
      "href",
      "/projects/new/",
    );
    // The same request as Home's: one, for both.
    const mine = calls("GET", "/api/v1/projects").filter((c) => c.query.get("member") === "true");
    expect(Object.fromEntries(mine[0]?.query ?? [])).toEqual({
      sort: "name",
      limit: "50",
      member: "true",
      archived: "false",
    });
  });

  it("offers no new project to someone who may not make one", async () => {
    signedIn({ can: { ...ME.can, create_projects: false } });
    answer("GET /api/v1/projects", page([]));
    await renderApp("/projects/");
    expect(within(nav()).queryByRole("link", { name: "New project" })).toBeNull();
  });

  it("lists the reader's saved searches, each opening from its own address", async () => {
    signedIn();
    answer("GET /api/v1/projects", page([project()]));
    answer("GET /api/v1/saved-searches", [saved()]);
    await renderApp("/projects/");
    const view = await within(nav()).findByRole("link", { name: "Ampicillin plasmids" });
    expect(view).toHaveAttribute("href", "/saved-searches/00000000-0000-7000-8000-0000000000s1/");
    const asked = calls("GET", "/api/v1/saved-searches")[0];
    // Their own only: a search others shared is in its project's list, not here.
    expect(Object.fromEntries(asked?.query ?? [])).toEqual({ mine: "true", limit: "8" });
    expect(within(nav()).getByRole("group", { name: "Saved views" })).toContainElement(view);
  });

  it("opens a saved search on its project's grid", async () => {
    signedIn();
    const atlas = project();
    answer("GET /api/v1/projects", page([atlas]));
    answer(`GET /api/v1/projects/${atlas.id}`, atlas);
    answer("GET /api/v1/saved-searches/00000000-0000-7000-8000-0000000000s1", saved());
    answer("GET /api/v1/entity-types", []);
    answer("POST /api/v1/entities/search", { items: [], next_cursor: null, total: 0 });
    const { router } = await renderApp("/saved-searches/00000000-0000-7000-8000-0000000000s1/");
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/projects/kinase-atlas/entities/");
    });
    expect(router.state.location.search).toEqual({ q: "ampicillin", show: "all" });
  });

  it("opens a search of every project on All entities", async () => {
    signedIn();
    answer("GET /api/v1/projects", page([project()]));
    answer(
      "GET /api/v1/saved-searches/00000000-0000-7000-8000-0000000000s1",
      saved({ project: null }),
    );
    answer("GET /api/v1/entity-types", []);
    answer("POST /api/v1/entities/search", { items: [], next_cursor: null, total: 0 });
    const { router } = await renderApp("/saved-searches/00000000-0000-7000-8000-0000000000s1/");
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/entities/");
    });
    expect(router.state.location.search).toEqual({ q: "ampicillin", show: "all" });
  });
});

describe("which item is current", () => {
  it("is the most specific", () => {
    const links = [
      {
        key: "all",
        label: "All projects",
        href: "/projects/",
        app: true,
        prefixes: ["/projects/"],
      },
      {
        key: "atlas",
        label: "Kinase Atlas",
        href: "/projects/kinase-atlas/",
        app: true,
        prefixes: ["/projects/kinase-atlas/"],
      },
    ];
    expect(currentOf("/projects/kinase-atlas/members/", links)).toBe("atlas");
    expect(currentOf("/projects/", links)).toBe("all");
    expect(currentOf("/work/", links)).toBeUndefined();
  });

  it("offers Admin, to the admin area, to whoever may open any page of it", () => {
    const none = Object.fromEntries(Object.keys(ME.can).map((k) => [k, false])) as typeof ME.can;
    expect(adminLink({ ...ME, can: none })).toBeNull();
    expect(adminLink({ ...ME, can: { ...none, read_audit: true } })).toMatchObject({
      label: "Admin",
      href: "/admin/",
      prefixes: ["/admin/"],
    });
  });

  it("names a person by their initials", () => {
    expect(initials("Rosalind Elsie Franklin")).toBe("RE");
    expect(initials("ada@example.com")).toBe("A");
  });
});

describe("the account menu", () => {
  it("closes on Escape, and the focus goes back to its button", async () => {
    signedIn();
    answer("GET /api/v1/projects", page([]));
    const { user } = await renderApp("/projects/");
    const account = screen.getByRole("button", { name: "Rosalind Franklin" });
    await user.click(account);
    expect(screen.getByRole("link", { name: "Notification preferences" })).toHaveFocus();
    await user.keyboard("{Escape}");
    expect(account).toHaveAttribute("aria-expanded", "false");
    expect(account).toHaveFocus();
    expect(screen.queryByRole("button", { name: "Sign out" })).toBeNull();
  });
});

describe("the drawer", () => {
  it("opens as a dialog, keeps the page out of reach, and closes on Escape", async () => {
    signedIn();
    answer("GET /api/v1/projects", page([]));
    const { user } = await renderApp("/projects/");
    const menu = screen.getByRole("button", { name: "Menu" });
    await user.click(menu);
    const drawer = screen.getByRole("dialog", { name: "Menu" });
    expect(within(drawer).getByRole("button", { name: "Close menu" })).toHaveFocus();
    expect(document.getElementById("main")).toHaveProperty("inert", true);
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.getElementById("main")).toHaveProperty("inert", false);
    expect(menu).toHaveFocus();
  });

  it("closes when the reader goes somewhere", async () => {
    signedIn();
    answer("GET /api/v1/projects", page([]));
    answer("GET /api/v1/entity-types", []);
    const { user } = await renderApp("/projects/");
    await user.click(screen.getByRole("button", { name: "Menu" }));
    await user.click(
      within(screen.getByRole("dialog", { name: "Menu" })).getByRole("link", { name: "Types" }),
    );
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
  });
});

describe("search everything", () => {
  it("opens the palette from its button, as ⌘K and Ctrl+K do: once each", async () => {
    signedIn();
    answer("GET /api/v1/projects", page([]));
    answer("GET /api/v1/search", {
      q: "",
      groups: [],
      searched_records: false,
      recent: [],
      projects_readable: 0,
    });
    const { user } = await renderApp("/projects/");
    const asked = vi.fn();
    window.addEventListener("iki:search", asked);
    try {
      for (const open of [
        () => user.click(screen.getByRole("button", { name: "Search everything" })),
        () => user.keyboard("{Meta>}k{/Meta}"),
        () => user.keyboard("{Control>}k{/Control}"),
      ]) {
        await open();
        expect(await screen.findByRole("dialog", { name: "Search everything" })).toBeVisible();
        await user.keyboard("{Escape}");
        await waitFor(() => {
          expect(screen.queryByRole("dialog", { name: "Search everything" })).toBeNull();
        });
      }
      // The sidebar binds no shortcut of its own: the palette's opens it, once.
      expect(asked).toHaveBeenCalledTimes(3);
    } finally {
      window.removeEventListener("iki:search", asked);
    }
  });
});
