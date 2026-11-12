/**
 * The app's pages against a fake API: what people see, and the calls each
 * action makes. The browser tests (server/tests/test_browser.py) run the real
 * thing end to end, with axe and the production CSP.
 */

import { QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, RouterProvider } from "@tanstack/react-router";
import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
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
  delivery,
  webhook,
  WEBHOOK_EVENTS,
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

/** The settings card with this heading. */
function card(heading: string): HTMLElement {
  const found = screen.getByRole("heading", { name: heading }).closest("section");
  if (!found) throw new Error(`no ${heading} card`);
  return found;
}

function onProjectPage(overrides: Parameters<typeof project>[0] = {}) {
  const found = project(overrides);
  answer("GET /api/v1/projects", ({ query }: { query: URLSearchParams }) =>
    page(query.get("slug") === found.slug || !query.has("slug") ? [found] : []),
  );
  return found;
}

describe("the frame", () => {
  it("shows the navigation each person may use", async () => {
    signedIn({
      can: { manage_org: true, read_audit: false, create_projects: true, manage_webhooks: false },
    });
    answer("GET /api/v1/projects", page([]));
    await renderApp("/projects/");
    const nav = screen.getByRole("navigation", { name: "Main" });
    const links = within(nav)
      .getAllByRole("link")
      .map((a) => a.textContent);
    expect(links).toEqual(["Projects", "Types", "Teams", "People", "Settings"]);
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
    signedIn({
      can: { manage_org: true, read_audit: false, create_projects: true, manage_webhooks: false },
    });
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
    signedIn({
      can: { manage_org: true, read_audit: true, create_projects: false, manage_webhooks: false },
    });
    answer("GET /api/v1/projects", page([]));
    await renderApp("/");
    expect(await screen.findByText("Start by creating a team")).toBeVisible();
    expect(screen.queryByRole("link", { name: "New project" })).toBeNull();
  });

  it("tells everyone else whom to ask", async () => {
    signedIn({
      can: { manage_org: false, read_audit: false, create_projects: false, manage_webhooks: false },
    });
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
    signedIn({
      can: { manage_org: false, read_audit: false, create_projects: false, manage_webhooks: false },
    });
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
    signedIn({
      can: { manage_org: true, read_audit: false, create_projects: false, manage_webhooks: false },
    });
    onProjectPage({
      my_role: null,
      can: {
        read: false,
        edit: false,
        manage: false,
        manage_members: true,
        manage_webhooks: false,
        run: [],
      },
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
    onProjectPage({
      my_role: "viewer",
      can: {
        read: true,
        edit: false,
        manage: false,
        manage_members: false,
        manage_webhooks: false,
        run: [],
      },
    });
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

  it("restricts sharing without asking, and shows it's restricted", async () => {
    signedIn();
    onProjectPage();
    answer(`PATCH /api/v1/projects/${PROJECT.id}`, project({ allow_sharing: false }));
    const { user } = await renderApp("/projects/kinase-atlas/settings/");
    const sharing = screen.getByRole("checkbox", {
      name: "Allow sharing this project’s entities with other projects",
    });
    expect(sharing).toBeChecked();
    expect(sharing).toHaveAccessibleDescription(/Use this for partner or CRO data/);
    await user.click(sharing);
    onProjectPage({ allow_sharing: false });
    await user.click(within(card("Sharing")).getByRole("button", { name: "Save" }));
    expect(
      await screen.findByText("Kinase Atlas’s entities can’t be shared outside it now."),
    ).toBeVisible();
    expect(calls("PATCH")[0]?.body).toEqual({ allow_sharing: false });
    expect(await screen.findByText("Not shared outside this project")).toBeVisible();
  });

  it("asks before allowing sharing again", async () => {
    signedIn();
    onProjectPage({ allow_sharing: false });
    answer(`PATCH /api/v1/projects/${PROJECT.id}`, project());
    const { user } = await renderApp("/projects/kinase-atlas/settings/");
    expect(screen.getByText("Not shared outside this project")).toBeVisible();
    await user.click(screen.getByRole("checkbox", { name: /Allow sharing/ }));
    await user.click(within(card("Sharing")).getByRole("button", { name: "Save" }));
    const dialog = screen.getByRole("alertdialog", {
      name: "Allow sharing Kinase Atlas’s entities?",
    });
    expect(calls("PATCH")).toEqual([]);
    onProjectPage();
    await user.click(within(dialog).getByRole("button", { name: "Allow sharing" }));
    expect(
      await screen.findByText("Kinase Atlas’s entities can be shared with other projects."),
    ).toBeVisible();
    expect(calls("PATCH")[0]?.body).toEqual({ allow_sharing: true });
  });

  it("is for Admins only", async () => {
    signedIn();
    onProjectPage({
      my_role: "editor",
      can: {
        read: true,
        edit: false,
        manage: false,
        manage_members: false,
        manage_webhooks: false,
        run: [],
      },
    });
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

  it("says when the stream finds the session ended, without leaving the page", async () => {
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
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("You’ve been signed out.");
    expect(within(alert).getByRole("link", { name: "Sign in again" })).toHaveAttribute(
      "href",
      "/login/?next=%2Fprojects%2F",
    );
    expect(assign).not.toHaveBeenCalled();
    expect(FakeEventSource.current()).toBeUndefined();
  });

  it("checks who's signed in when the server refuses the stream", async () => {
    signedIn();
    answer("GET /api/v1/projects", page([]));
    await renderApp("/projects/");
    answer("GET /api/v1/me", problem(401, "not_authenticated", "Sign in."));
    act(() => {
      FakeEventSource.current()?.fail();
    });
    expect(await screen.findByRole("alert")).toHaveTextContent("You’ve been signed out.");
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

describe("webhooks", () => {
  const HOOK = webhook();
  const HOOKS = `/api/v1/projects/${PROJECT.id}/webhooks`;
  const ORG_ADMIN = {
    can: { manage_org: true, read_audit: true, create_projects: true, manage_webhooks: true },
  };

  function onWebhooks(hooks = [HOOK]) {
    onProjectPage();
    answer(`GET ${HOOKS}`, page(hooks));
    answer("GET /api/v1/webhook-events", WEBHOOK_EVENTS);
    answer("GET /api/v1/webhook-policy", { project_webhooks: true });
  }

  function onWebhook(found = HOOK, deliveries = [delivery()]) {
    onProjectPage();
    answer(`GET /api/v1/webhooks/${found.id}`, found);
    answer("GET /api/v1/webhook-events", WEBHOOK_EVENTS);
    answer(`GET /api/v1/webhooks/${found.id}/deliveries`, page(deliveries));
  }

  it("are a project tab for its Admins only", async () => {
    signedIn();
    onWebhooks();
    await renderApp("/projects/kinase-atlas/webhooks/");
    const tabs = screen.getByRole("navigation", { name: "Project" });
    expect(within(tabs).getByRole("link", { name: "Webhooks" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    const row = (await screen.findByRole("link", { name: "eln.example.com" })).closest("li");
    expect(row).toHaveTextContent("Our ELN · 2 events");
    expect(within(row as HTMLElement).getByText("On")).toBeVisible();
    expect(document.title).toBe("Webhooks · Kinase Atlas · Example Bio");

    cleanup();
    queryClient.clear();
    onProjectPage({
      my_role: "editor",
      can: {
        read: true,
        edit: false,
        manage: false,
        manage_members: false,
        manage_webhooks: false,
        run: [],
      },
    });
    await renderApp("/projects/kinase-atlas/webhooks/");
    expect(
      within(screen.getByRole("navigation", { name: "Project" })).queryByRole("link", {
        name: "Webhooks",
      }),
    ).toBeNull();
    expect(screen.getByText("Only the project’s Admins can manage its webhooks.")).toBeVisible();
  });

  it("checks the address and events before adding one", async () => {
    signedIn();
    onWebhooks([]);
    const { user } = await renderApp("/projects/kinase-atlas/webhooks/");
    await user.click(await screen.findByRole("button", { name: "Add webhook" }));
    expect(screen.getByText("Enter the address to send events to.")).toBeVisible();
    expect(screen.getByText("Choose at least one event.")).toBeVisible();
    expect(screen.getByRole("textbox", { name: /Address/ })).toHaveFocus();
    expect(calls("POST")).toEqual([]);
  });

  it("adds one and shows its secret once", async () => {
    signedIn();
    onWebhooks([]);
    const secret = "whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw";
    answer(`POST ${HOOKS}`, (call: { body: unknown }) => {
      answer(`GET ${HOOKS}`, page([HOOK]));
      return respond(201, { ...HOOK, ...(call.body as object), secret });
    });
    const { user } = await renderApp("/projects/kinase-atlas/webhooks/");
    await user.type(
      await screen.findByRole("textbox", { name: /Address/ }),
      " https://eln.example.com/hooks/iki ",
    );
    await user.click(screen.getByRole("checkbox", { name: /project\.updated/ }));
    await user.click(screen.getByRole("button", { name: "Add webhook" }));
    expect(await screen.findByText("Copy the signing secret now.")).toBeVisible();
    expect(calls("POST", HOOKS)[0]?.body).toEqual({
      url: "https://eln.example.com/hooks/iki",
      description: "",
      events: ["project.updated"],
    });
    expect(screen.getByText(secret)).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Copy signing secret" }));
    expect(await navigator.clipboard.readText()).toBe(secret);
    expect(screen.getByRole("button", { name: "Copied" })).toBeVisible();
    expect(
      screen.getByText("Added a webhook to eln.example.com, and sent it a test message."),
    ).toBeVisible();
    // Gone once dismissed: it's never shown again.
    await user.click(screen.getByRole("button", { name: "Add another" }));
    expect(screen.queryByText(secret)).toBeNull();
  });

  it("shows a refused address next to the field", async () => {
    signedIn();
    onWebhooks([]);
    answer(
      `POST ${HOOKS}`,
      problem(409, "refused", "Iki doesn’t send to private addresses.", [
        { pointer: "#/url", detail: "Iki doesn’t send to private addresses." },
      ]),
    );
    const { user } = await renderApp("/projects/kinase-atlas/webhooks/");
    await user.type(await screen.findByRole("textbox", { name: /Address/ }), "https://10.0.0.1/");
    await user.click(screen.getByRole("checkbox", { name: /project\.updated/ }));
    await user.click(screen.getByRole("button", { name: "Add webhook" }));
    expect(await screen.findByText("Iki doesn’t send to private addresses.")).toBeVisible();
    expect(screen.getByRole("textbox", { name: /Address/ })).toHaveAttribute(
      "aria-invalid",
      "true",
    );
  });

  it("says when the Org Admins have turned project webhooks off", async () => {
    signedIn();
    onWebhooks();
    answer("GET /api/v1/webhook-policy", { project_webhooks: false });
    await renderApp("/projects/kinase-atlas/webhooks/");
    expect(await screen.findByText(/so these don’t send/)).toBeVisible();
    expect(screen.queryByRole("button", { name: "Add webhook" })).toBeNull();
  });

  it("sends a test message and turns off", async () => {
    signedIn();
    onWebhook();
    answer(`POST /api/v1/webhooks/${HOOK.id}/ping`, respond(202, delivery({ event: "ping" })));
    answer(`PATCH /api/v1/webhooks/${HOOK.id}`, () => {
      answer(`GET /api/v1/webhooks/${HOOK.id}`, { ...HOOK, active: false });
      return { ...HOOK, active: false };
    });
    const { user } = await renderApp(`/projects/kinase-atlas/webhooks/${HOOK.id}/`);
    expect(
      await screen.findByRole("heading", { name: /Webhook to eln\.example\.com/ }),
    ).toBeVisible();
    expect(document.title).toBe("Webhook to eln.example.com · Kinase Atlas · Example Bio");
    await user.click(screen.getByRole("button", { name: "Send a test message" }));
    expect(await screen.findByText(/Sent a test message/)).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Turn off" }));
    expect(await screen.findByRole("button", { name: "Turn on" })).toBeVisible();
    expect(calls("PATCH")[0]?.body).toEqual({ active: false });
  });

  it("says why it's off, and leaves an Org Admin's switch to them", async () => {
    signedIn();
    onWebhook({ ...HOOK, active: false, disabled_reason: "org_admin" });
    await renderApp(`/projects/kinase-atlas/webhooks/${HOOK.id}/`);
    expect(await screen.findByText(/Only an Org Admin can turn it back on/)).toBeVisible();
    expect(screen.queryByRole("button", { name: "Turn on" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Send a test message" })).toBeNull();
  });

  it("explains a webhook Iki turned off for failing", async () => {
    signedIn();
    onWebhook({
      ...HOOK,
      active: false,
      disabled_reason: "failing",
      failing_since: "2026-03-09T09:00:00Z",
    });
    await renderApp(`/projects/kinase-atlas/webhooks/${HOOK.id}/`);
    const alert = await screen.findByText(/so Iki turned this webhook off/);
    expect(alert).toHaveTextContent("since 9 Mar 2026");
    expect(screen.getByRole("button", { name: "Turn on" })).toBeVisible();
  });

  it("asks before rotating the secret, then shows the new one once", async () => {
    signedIn();
    onWebhook();
    answer(`POST /api/v1/webhooks/${HOOK.id}/rotate-secret`, {
      secret: "whsec_new",
      previous_secret_expires_at: "2026-03-15T09:00:00Z",
    });
    const { user } = await renderApp(`/projects/kinase-atlas/webhooks/${HOOK.id}/`);
    await user.click(await screen.findByRole("button", { name: "Rotate secret" }));
    const dialog = screen.getByRole("alertdialog", { name: "Rotate the signing secret?" });
    expect(calls("POST")).toEqual([]);
    await user.click(within(dialog).getByRole("button", { name: "Rotate secret" }));
    expect(await screen.findByText("whsec_new")).toBeVisible();
    expect(screen.getByText(/Copy the new secret now/).closest("p")).toHaveTextContent(
      "Until 15 Mar 2026",
    );
  });

  it("asks before deleting, then goes back to the list", async () => {
    signedIn();
    onWebhook();
    answer(`GET ${HOOKS}`, page([]));
    answer("GET /api/v1/webhook-policy", { project_webhooks: true });
    answer(`DELETE /api/v1/webhooks/${HOOK.id}`, respond(204));
    const { router, user } = await renderApp(`/projects/kinase-atlas/webhooks/${HOOK.id}/`);
    await user.click(await screen.findByRole("button", { name: "Delete webhook" }));
    const dialog = screen.getByRole("alertdialog", {
      name: "Delete the webhook to eln.example.com?",
    });
    await user.click(within(dialog).getByRole("button", { name: "Delete webhook" }));
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/projects/kinase-atlas/webhooks/");
    });
    expect(await screen.findByText("Deleted the webhook to eln.example.com.")).toBeVisible();
  });

  it("shows what each delivery sent and how it went, and sends it again", async () => {
    signedIn();
    const sent = delivery();
    onWebhook(HOOK, [sent]);
    answer(`GET /api/v1/webhooks/${HOOK.id}/deliveries/${sent.id}`, sent);
    answer(
      `POST /api/v1/webhooks/${HOOK.id}/deliveries/${sent.id}/redeliver`,
      respond(202, { ...sent, status: "pending" }),
    );
    const { user } = await renderApp(`/projects/kinase-atlas/webhooks/${HOOK.id}/`);
    const row = (await screen.findByText("Failed")).closest("li") as HTMLElement;
    expect(row).toHaveTextContent("2 attempts · Timed out after 15 seconds.");
    const details = within(row).getByRole("button", { name: "Details of project.updated" });
    expect(details).toHaveAttribute("aria-expanded", "false");
    await user.click(details);
    expect(details).toHaveAttribute("aria-expanded", "true");
    expect(await within(row).findByText("msg_2dQ8Zx")).toBeVisible();
    expect(within(row).getByText(/"name": "Kinase Atlas"/)).toBeVisible();
    expect(within(row).getByText(/503 · 41 ms/)).toBeVisible();
    await user.click(within(row).getByRole("button", { name: "Send again" }));
    expect(await screen.findByText("Sent the project.updated message again.")).toBeVisible();
  });

  it("are Org Admins' to manage in settings", async () => {
    signedIn();
    answer("GET /api/v1/projects", page([]));
    await renderApp("/settings/webhooks/");
    expect(
      screen.getByText("Only Org Admins can manage the organization’s webhooks."),
    ).toBeVisible();
  });

  it("let Org Admins see where project webhooks send, and turn them off", async () => {
    signedIn(ORG_ADMIN);
    answer("GET /api/v1/webhooks", page([webhook({ scope: "org", project: null })]));
    answer("GET /api/v1/webhook-events", WEBHOOK_EVENTS);
    answer("GET /api/v1/webhook-policy", { project_webhooks: true });
    const listed = {
      id: HOOK.id,
      project: HOOK.project,
      host: "lims.example.com",
      events: ["project.updated"],
      active: true,
      disabled_reason: "",
      created_by: HOOK.created_by,
      created_at: HOOK.created_at,
    };
    answer("GET /api/v1/project-webhooks", page([listed]));
    answer(`PATCH /api/v1/webhooks/${HOOK.id}`, () => {
      answer(
        "GET /api/v1/project-webhooks",
        page([{ ...listed, active: false, disabled_reason: "org_admin" }]),
      );
      return { ...HOOK, active: false, disabled_reason: "org_admin" };
    });
    const { user } = await renderApp("/settings/webhooks/");
    const nav = screen.getByRole("navigation", { name: "Main" });
    expect(within(nav).getByRole("link", { name: "Settings" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    const tabs = screen.getByRole("navigation", { name: "Settings" });
    expect(within(tabs).getByRole("link", { name: "Webhooks" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(await screen.findByRole("link", { name: "eln.example.com" })).toHaveAttribute(
      "href",
      `/settings/webhooks/${HOOK.id}/`,
    );
    const row = (await screen.findByText("lims.example.com")).closest("li") as HTMLElement;
    expect(within(row).getByRole("link", { name: "Kinase Atlas" })).toBeVisible();
    await user.click(
      within(row).getByRole("button", {
        name: "Turn off the webhook to lims.example.com in Kinase Atlas",
      }),
    );
    expect(await within(row).findByText("Off: turned off by an Org Admin")).toBeVisible();
    expect(calls("PATCH")[0]?.body).toEqual({ active: false });
    expect(
      within(row).getByRole("button", { name: /Turn on the webhook to lims\.example\.com/ }),
    ).toBeVisible();
  });

  it("let Org Admins choose who may add project webhooks", async () => {
    signedIn(ORG_ADMIN);
    answer("GET /api/v1/webhooks", page([]));
    answer("GET /api/v1/webhook-events", WEBHOOK_EVENTS);
    answer("GET /api/v1/webhook-policy", { project_webhooks: true });
    answer("GET /api/v1/project-webhooks", page([]));
    answer("PATCH /api/v1/webhook-policy", { project_webhooks: false });
    const { user } = await renderApp("/settings/webhooks/");
    await user.click(await screen.findByRole("radio", { name: /No one/ }));
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText("Saved.")).toBeVisible();
    expect(calls("PATCH", "/api/v1/webhook-policy")[0]?.body).toEqual({ project_webhooks: false });
  });
});
