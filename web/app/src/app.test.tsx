/**
 * The app's pages against a fake API: what people see, and the calls each
 * action makes. The browser tests (server/tests/test_browser.py) run the real
 * thing end to end, with axe and the production CSP.
 */

import { QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, RouterProvider } from "@tanstack/react-router";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { queryClient } from "./api";
import { createAppRouter } from "./router";
import {
  answer,
  calls,
  FakeEventSource,
  ME,
  notification,
  page,
  problem,
  project,
  respond,
  signedIn,
  TEAM,
} from "./test-server";

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

const PROJECT = project();
const MEMBERS = `/api/v1/projects/${PROJECT.id}/members`;

function onProjectPage(overrides: Parameters<typeof project>[0] = {}) {
  const found = project(overrides);
  answer("GET /api/v1/projects", ({ query }: { query: URLSearchParams }) =>
    page(query.get("slug") === found.slug || !query.has("slug") ? [found] : []),
  );
  return found;
}

describe("the frame", () => {
  it("shows the navigation each person may use", async () => {
    signedIn({ can: { manage_org: true, read_audit: false, create_projects: true } });
    answer("GET /api/v1/projects", page([]));
    await renderApp("/projects/");
    const nav = screen.getByRole("navigation", { name: "Main" });
    const links = within(nav)
      .getAllByRole("link")
      .map((a) => a.textContent);
    expect(links).toEqual(["Projects", "Teams", "People", "Settings"]);
    expect(within(nav).getByRole("link", { name: "Projects" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(document.title).toBe("Projects · Example Bio");
  });

  it("links to the person's account and their notifications", async () => {
    signedIn();
    answer("GET /api/v1/notifications/summary", { unread: 3 });
    answer("GET /api/v1/projects", page([]));
    await renderApp("/projects/");
    expect(screen.getByRole("link", { name: "Rosalind Franklin" })).toHaveAttribute(
      "href",
      "/account/",
    );
    const bell = await screen.findByRole("link", { name: "Notifications, 3 unread" });
    expect(bell).toHaveAttribute("href", "/notifications/");
  });

  it("signs out with a form post carrying the CSRF token", async () => {
    signedIn();
    answer("GET /api/v1/projects", page([]));
    await renderApp("/projects/");
    const button = screen.getByRole("button", { name: "Sign out" });
    const form = button.closest("form");
    expect(form).toHaveAttribute("action", "/logout/");
    expect(form).toHaveAttribute("method", "post");
    expect(form?.querySelector('input[name="csrfmiddlewaretoken"]')).toHaveValue("test-csrf-token");
  });

  it("says when there's no such page", async () => {
    signedIn();
    answer("GET /api/v1/projects", page([]));
    await renderApp("/projects/nope/");
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Not found");
  });
});

describe("home", () => {
  it("welcomes people and lists their active projects", async () => {
    signedIn({ can: { manage_org: true, read_audit: false, create_projects: true } });
    answer(
      "GET /api/v1/projects",
      page([
        PROJECT,
        project({ id: "p2", name: "Old Screen", slug: "old-screen", archived: true }),
        project({ id: "p3", name: "Imaging", slug: "imaging", my_role: null }),
      ]),
    );
    await renderApp("/");
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Welcome, Rosalind");
    const rows = await screen.findAllByRole("listitem");
    expect(rows).toHaveLength(1);
    expect(rows[0]).toHaveTextContent("Kinase Atlas");
    expect(rows[0]).toHaveTextContent("Structural Biology");
    expect(rows[0]).toHaveTextContent("Admin");
    expect(screen.getByText(/can also see 1 project you’re not a member of/)).toBeVisible();
    expect(screen.getByRole("link", { name: "New project" })).toHaveAttribute(
      "href",
      "/projects/new/",
    );
  });

  it("points Org Admins without a team at teams", async () => {
    signedIn({ can: { manage_org: true, read_audit: true, create_projects: false } });
    answer("GET /api/v1/projects", page([]));
    await renderApp("/");
    expect(await screen.findByText("Start by creating a team")).toBeVisible();
    expect(screen.queryByRole("link", { name: "New project" })).toBeNull();
  });

  it("tells everyone else whom to ask", async () => {
    signedIn({ can: { manage_org: false, read_audit: false, create_projects: false } });
    answer("GET /api/v1/projects", page([]));
    await renderApp("/");
    expect(await screen.findByText("You’re not in any projects yet")).toBeVisible();
  });
});

describe("a new project", () => {
  it("checks the name before sending, and focuses it", async () => {
    signedIn();
    answer("GET /api/v1/teams", page([TEAM]));
    const { user } = await renderApp("/projects/new/");
    await user.click(screen.getByRole("button", { name: "Create project" }));
    const name = screen.getByRole("textbox", { name: "Project name" });
    expect(name).toHaveAttribute("aria-invalid", "true");
    expect(name).toHaveAccessibleDescription("Enter a name for the project.");
    expect(name).toHaveFocus();
    expect(screen.getByRole("alert")).toHaveTextContent("Check the highlighted fields.");
    expect(calls("POST")).toEqual([]);
  });

  it("creates it in the chosen team and opens it", async () => {
    signedIn();
    const other = { id: "t2", name: "Chemistry", slug: "chemistry" };
    answer("GET /api/v1/teams", page([TEAM, other]));
    const created = project({ name: "Variant Calls", slug: "variant-calls" });
    answer("POST /api/v1/projects", respond(201, created));
    onProjectPage({ name: "Variant Calls", slug: "variant-calls" });
    const { router, user } = await renderApp("/projects/new/?team=chemistry");

    expect(screen.getByRole("combobox", { name: "Team" })).toHaveValue("t2");
    await user.type(screen.getByRole("textbox", { name: "Project name" }), "  Variant Calls ");
    await user.click(screen.getByRole("button", { name: "Create project" }));

    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/projects/variant-calls/");
    });
    const [post] = calls("POST", "/api/v1/projects");
    expect(post?.body).toEqual({ team: "t2", name: "Variant Calls", description: "" });
    expect(post?.headers.get("X-CSRFToken")).toBe("test-csrf-token");
    expect(await screen.findByText("Created Variant Calls. You’re its Admin.")).toBeVisible();
  });

  it("shows a refusal next to the field it's about", async () => {
    signedIn();
    answer("GET /api/v1/teams", page([TEAM]));
    const message = "Structural Biology already has a project called “Kinase Atlas”.";
    answer(
      "POST /api/v1/projects",
      problem(409, "refused", message, [{ pointer: "#/name", detail: message }]),
    );
    const { user } = await renderApp("/projects/new/");
    await user.type(screen.getByRole("textbox", { name: "Project name" }), "Kinase Atlas");
    await user.click(screen.getByRole("button", { name: "Create project" }));
    const name = screen.getByRole("textbox", { name: "Project name" });
    await waitFor(() => {
      expect(name).toHaveAccessibleDescription(message);
    });
    expect(name).toHaveFocus();
  });

  it("explains how to get a team", async () => {
    signedIn({ can: { manage_org: false, read_audit: false, create_projects: false } });
    answer("GET /api/v1/teams", page([]));
    await renderApp("/projects/new/");
    expect(
      screen.getByText("Projects belong to teams, and you’re not in a team yet."),
    ).toBeVisible();
    expect(screen.getByText("Ask an Org Admin to add you to a team.")).toBeVisible();
  });
});

describe("a project", () => {
  it("shows your role and the project's tabs", async () => {
    signedIn();
    onProjectPage({ description: "Kinases\nand their inhibitors" });
    await renderApp("/projects/kinase-atlas/");
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Kinase Atlas");
    const tabs = screen.getByRole("navigation", { name: "Project" });
    expect(within(tabs).getByRole("link", { name: "Overview" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(within(tabs).getByRole("link", { name: "Settings" })).toBeVisible();
    expect(await screen.findByText(/Also manages members and settings/)).toBeVisible();
    expect(screen.getByText("14 Mar 2026")).toBeVisible();
  });

  it("lets an Org Admin who isn't a member add themselves", async () => {
    signedIn({ can: { manage_org: true, read_audit: false, create_projects: false } });
    onProjectPage({
      my_role: null,
      can: { read: false, manage: false, manage_members: true },
    });
    answer(`GET ${MEMBERS}`, page([]));
    answer(`PUT ${MEMBERS}/users/${ME.id}`, {});
    const { user } = await renderApp("/projects/kinase-atlas/");
    expect(screen.getByText("You’re not a member of this project")).toBeVisible();
    expect(await screen.findByText("This project has no Admins.")).toBeVisible();
    const tabs = screen.getByRole("navigation", { name: "Project" });
    expect(within(tabs).queryByRole("link", { name: "Settings" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Add me as Admin" }));
    await waitFor(() => {
      expect(calls("PUT")).toHaveLength(1);
    });
    expect(calls("PUT")[0]?.body).toEqual({ role: "admin" });
    expect(await screen.findByText("You’re now an Admin of Kinase Atlas.")).toBeVisible();
  });
});

describe("members", () => {
  const ADA = {
    id: "u2",
    email: "ada@example.com",
    name: "Ada Lovelace",
    kind: "person" as const,
    active: true,
  };
  const people = [
    {
      id: "m1",
      role: "admin" as const,
      user: { ...ME, org_roles: undefined, can: undefined },
      team: null,
      team_size: null,
      added_at: "2026-03-14T09:00:00Z",
    },
    {
      id: "m2",
      role: "viewer" as const,
      user: ADA,
      team: null,
      team_size: null,
      added_at: "2026-03-14T09:00:00Z",
    },
    {
      id: "m3",
      role: "editor" as const,
      user: null,
      team: { id: "t2", name: "Chemistry", slug: "chemistry" },
      team_size: 1,
      added_at: "2026-03-14T09:00:00Z",
    },
  ];

  it("shows people and teams, read-only to members", async () => {
    signedIn();
    onProjectPage({ my_role: "viewer", can: { read: true, manage: false, manage_members: false } });
    answer(`GET ${MEMBERS}`, page(people));
    await renderApp("/projects/kinase-atlas/members/");
    const list = await screen.findByRole("list", { name: "Members" });
    expect(within(list).getByText("(you)")).toBeVisible();
    expect(within(list).getByText(/Everyone in the team · 1 member$/)).toBeVisible();
    expect(screen.queryByRole("button", { name: "Remove" })).toBeNull();
    expect(screen.getByText("What each role can do")).toBeVisible();
  });

  it("says why someone can't be added, without calling the API to add them", async () => {
    signedIn();
    onProjectPage();
    answer(`GET ${MEMBERS}`, page(people));
    answer(`GET /api/v1/projects/${PROJECT.id}/grantable-teams`, page([]));
    answer("GET /api/v1/users", page([]));
    const { user } = await renderApp("/projects/kinase-atlas/members/");
    await user.type(screen.getByRole("textbox", { name: "Email address" }), "new@example.com");
    await user.click(screen.getByRole("button", { name: "Add person" }));
    const email = screen.getByRole("textbox", { name: "Email address" });
    await waitFor(() => {
      expect(email).toHaveAccessibleDescription(
        expect.stringContaining("No one with that email address has signed in to Iki yet."),
      );
    });
    expect(calls("PUT")).toEqual([]);
  });

  it("adds a person with the chosen role", async () => {
    signedIn();
    onProjectPage();
    answer(`GET ${MEMBERS}`, page(people));
    answer(`GET /api/v1/projects/${PROJECT.id}/grantable-teams`, page([]));
    answer("GET /api/v1/users", page([{ ...ADA, id: "u9", name: "Grace Hopper" }]));
    answer(`PUT ${MEMBERS}/users/u9`, {});
    const { user } = await renderApp("/projects/kinase-atlas/members/");
    await user.type(screen.getByRole("textbox", { name: "Email address" }), "grace@example.com");
    await user.click(screen.getByRole("radio", { name: /^Runner/ }));
    await user.click(screen.getByRole("button", { name: "Add person" }));
    expect(await screen.findByText("Grace Hopper is now a Runner.")).toBeVisible();
    expect(calls("PUT")[0]?.body).toEqual({ role: "runner" });
  });

  it("asks before removing someone", async () => {
    signedIn();
    onProjectPage();
    answer(`GET ${MEMBERS}`, page(people));
    answer(`GET /api/v1/projects/${PROJECT.id}/grantable-teams`, page([]));
    answer(`DELETE ${MEMBERS}/m2`, respond(204));
    const { user } = await renderApp("/projects/kinase-atlas/members/");
    const ada = (await screen.findByText("Ada Lovelace")).closest("li");
    if (!ada) throw new Error("no row");
    await user.click(within(ada).getByRole("button", { name: "Remove" }));
    const dialog = screen.getByRole("alertdialog", { name: "Remove Ada Lovelace?" });
    expect(calls("DELETE")).toEqual([]);
    await user.click(within(dialog).getByRole("button", { name: "Remove" }));
    expect(await screen.findByText("Removed Ada Lovelace from Kinase Atlas.")).toBeVisible();
    expect(calls("DELETE")).toHaveLength(1);
    expect(screen.queryByRole("alertdialog")).toBeNull();
  });

  it("shows a refused change on its row", async () => {
    signedIn();
    onProjectPage();
    answer(`GET ${MEMBERS}`, page(people));
    answer(`GET /api/v1/projects/${PROJECT.id}/grantable-teams`, page([]));
    const refusal = "A project needs at least one Admin. Make someone else Admin first.";
    answer(`PUT ${MEMBERS}/users/${ME.id}`, problem(409, "refused", refusal));
    const { user } = await renderApp("/projects/kinase-atlas/members/");
    await user.selectOptions(
      await screen.findByRole("combobox", { name: "Role for Rosalind Franklin" }),
      "viewer",
    );
    const mine = screen.getByText("(you)").closest("li");
    if (!mine) throw new Error("no row");
    await user.click(within(mine).getByRole("button", { name: "Change" }));
    expect(await within(mine).findByRole("alert")).toHaveTextContent(refusal);
  });
});

describe("settings", () => {
  it("asks before archiving, then shows the project archived", async () => {
    signedIn();
    onProjectPage();
    answer(`PATCH /api/v1/projects/${PROJECT.id}`, project({ archived: true }));
    const { router, user } = await renderApp("/projects/kinase-atlas/settings/");
    await user.click(screen.getByRole("button", { name: "Archive project" }));
    const dialog = screen.getByRole("alertdialog", { name: "Archive Kinase Atlas?" });
    onProjectPage({ archived: true });
    await user.click(within(dialog).getByRole("button", { name: "Archive project" }));
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/projects/kinase-atlas/");
    });
    expect(calls("PATCH")[0]?.body).toEqual({ archived: true });
    expect(await screen.findByText("Archived Kinase Atlas.")).toBeVisible();
    expect(await screen.findByText(/archived and read-only/)).toBeVisible();
  });

  it("is for Admins only", async () => {
    signedIn();
    onProjectPage({ my_role: "editor", can: { read: true, manage: false, manage_members: false } });
    await renderApp("/projects/kinase-atlas/settings/");
    expect(screen.getByText("Only the project’s Admins can change its settings.")).toBeVisible();
  });
});

describe("notifications", () => {
  beforeEach(() => {
    FakeEventSource.opened = [];
    vi.stubGlobal("EventSource", FakeEventSource);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  const UNREAD = notification();
  const READ = notification({
    id: "00000000-0000-7000-8000-0000000000n2",
    kind: "team.added",
    title: "You were added to Structural Biology",
    path: "/teams/structural-biology/",
    project: null,
    actor: null,
    created_at: new Date(Date.now() - 2 * 86_400_000).toISOString(),
    read_at: new Date().toISOString(),
  });

  it("lists them newest first, unread ones marked", async () => {
    signedIn();
    answer("GET /api/v1/notifications", page([UNREAD, READ]));
    await renderApp("/notifications/");
    const rows = await screen.findAllByRole("listitem");
    expect(rows[0]).toHaveTextContent("Unread: Maurice Wilkins added you to Kinase Atlas");
    expect(rows[0]).toHaveTextContent("Kinase Atlas · by Maurice Wilkins · 5 minutes ago");
    expect(rows[1]).not.toHaveTextContent("Unread");
    expect(rows[1]).toHaveTextContent("2 days ago");
    const older = rows[1];
    if (!older) throw new Error("expected two notifications");
    expect(within(older).getByRole("link")).toHaveAttribute("href", "/teams/structural-biology/");
    expect(screen.getByRole("link", { name: "Email settings" })).toHaveAttribute(
      "href",
      "/account/notifications/",
    );
    expect(calls("GET", "/api/v1/notifications")[0]?.query.has("unread")).toBe(false);
  });

  it("shows only unread ones when asked", async () => {
    signedIn();
    answer("GET /api/v1/notifications", page([]));
    await renderApp("/notifications/?show=unread");
    expect(await screen.findByText("You’re all caught up.")).toBeVisible();
    expect(calls("GET", "/api/v1/notifications")[0]?.query.get("unread")).toBe("true");
    expect(screen.getByRole("link", { name: "Unread" })).toHaveAttribute("aria-current", "page");
  });

  it("marks one read before opening it", async () => {
    signedIn();
    answer("GET /api/v1/notifications", page([UNREAD]));
    answer(`PATCH /api/v1/notifications/${UNREAD.id}`, { ...UNREAD, read_at: "now" });
    onProjectPage();
    const { router, user } = await renderApp("/notifications/");
    await user.click(await screen.findByRole("link", { name: UNREAD.title }));
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/projects/kinase-atlas/");
    });
    expect(calls("PATCH")[0]?.body).toEqual({ read: true });
  });

  it("marks read only what the person has seen", async () => {
    signedIn();
    answer("GET /api/v1/notifications", page([UNREAD, READ]));
    answer("POST /api/v1/notifications/read", { marked: 1, unread: 0 });
    const { user } = await renderApp("/notifications/");
    await user.click(await screen.findByRole("button", { name: "Mark all as read" }));
    expect(await screen.findByText("Marked 1 notification as read.")).toBeVisible();
    expect(calls("POST", "/api/v1/notifications/read")[0]?.body).toEqual({
      up_to: UNREAD.created_at,
    });
  });

  it("toggles one read and unread", async () => {
    signedIn();
    answer("GET /api/v1/notifications", page([READ]));
    answer(`PATCH /api/v1/notifications/${READ.id}`, { ...READ, read_at: null });
    const { user } = await renderApp("/notifications/");
    await user.click(await screen.findByRole("button", { name: `Mark unread: ${READ.title}` }));
    await waitFor(() => {
      expect(calls("PATCH")[0]?.body).toEqual({ read: false });
    });
  });

  it("updates the bell and the list as they arrive", async () => {
    signedIn();
    let inbox = [READ];
    answer("GET /api/v1/notifications", () => page(inbox));
    await renderApp("/notifications/");
    await screen.findAllByRole("listitem");
    const stream = FakeEventSource.current();
    expect(stream?.url).toBe("/api/v1/events");
    expect(stream?.init).toEqual({ withCredentials: true });

    inbox = [UNREAD, READ];
    act(() => {
      stream?.send("notification", { id: UNREAD.id, unread: 1 });
    });
    expect(await screen.findByRole("link", { name: "Notifications, 1 unread" })).toBeVisible();
    expect(await screen.findAllByRole("listitem")).toHaveLength(2);

    act(() => {
      stream?.send("notifications_read", { unread: 0 });
    });
    expect(await screen.findByRole("link", { name: "Notifications" })).toBeVisible();
  });

  it("sends the person to sign in when the stream says they're signed out", async () => {
    signedIn();
    answer("GET /api/v1/projects", page([]));
    const assign = vi.fn();
    vi.stubGlobal("location", {
      origin: window.location.origin,
      assign,
      pathname: "/projects/",
      search: "",
    });
    await renderApp("/projects/");
    act(() => {
      FakeEventSource.current()?.send("signed_out", {});
    });
    expect(assign).toHaveBeenCalledWith("/login/?next=%2Fprojects%2F");
    expect(FakeEventSource.current()).toBeUndefined();
  });

  it("tries again later when the server refuses the stream", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    signedIn();
    answer("GET /api/v1/projects", page([]));
    await renderApp("/projects/");
    act(() => {
      FakeEventSource.current()?.fail();
    });
    expect(FakeEventSource.current()).toBeUndefined();
    await act(() => vi.advanceTimersByTimeAsync(46_000));
    expect(FakeEventSource.current()).toBeDefined();
    expect(FakeEventSource.opened).toHaveLength(2);
  });

  it("closes the stream in a tab hidden for a while, and reopens it when shown", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    signedIn();
    answer("GET /api/v1/projects", page([]));
    await renderApp("/projects/");
    const visibility = vi.spyOn(document, "visibilityState", "get");
    visibility.mockReturnValue("hidden");
    act(() => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await act(() => vi.advanceTimersByTimeAsync(5 * 60_000 + 1));
    expect(FakeEventSource.current()).toBeUndefined();
    visibility.mockReturnValue("visible");
    act(() => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    expect(FakeEventSource.current()).toBeDefined();
    visibility.mockRestore();
  });
});
