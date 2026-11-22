/**
 * The bar across the top of the page (ADR 0176): which of the pages drawn at once fills
 * it, slot by slot, and the header that fills it, with its title, description and
 * toggletip. The browser tests measure it, in both themes and on a phone.
 */

import {
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
  Outlet,
  RouterProvider,
} from "@tanstack/react-router";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { TopBar, TopBarFrame } from "./page-top";
import { PageHeader } from "./ui";

/** A frame with the bar, a page of tabs in it (as a project's is), and a tab under that. */
async function renderPages(frame: ReactNode, tab: ReactNode, path = "/frame/tab") {
  const root = createRootRoute({
    component: () => (
      <main>
        <TopBarFrame>
          <div className="page-body">
            <Outlet />
          </div>
        </TopBarFrame>
      </main>
    ),
  });
  const frameRoute = createRoute({
    getParentRoute: () => root,
    path: "frame",
    component: () => (
      <>
        {frame}
        <Outlet />
      </>
    ),
  });
  const tabRoute = createRoute({
    getParentRoute: () => frameRoute,
    path: "tab",
    component: () => <>{tab}</>,
  });
  const router = createRouter({
    routeTree: root.addChildren([frameRoute.addChildren([tabRoute])]),
    history: createMemoryHistory({ initialEntries: [path] }),
  });
  render(<RouterProvider router={router} />);
  await screen.findByTestId("drawn");
  return router;
}

function bar(): HTMLElement {
  const found = document.querySelector<HTMLElement>(".page-top");
  if (!found) throw new Error("expected the bar");
  return found;
}

function crumbs(): string[] {
  return within(bar())
    .queryAllByRole("listitem")
    .map((li) => li.textContent);
}

describe("the bar across the top of the page", () => {
  it("holds a frame's real breadcrumb over a page's title alone, and the deepest actions", async () => {
    await renderPages(
      <TopBar
        crumbs={[<a href="/projects/">Projects</a>, "Kinase Atlas"]}
        actions={<button type="button">New</button>}
      />,
      <>
        <PageHeader title="Members" actions={<button type="button">Add member</button>} />
        <span data-testid="drawn" />
      </>,
    );
    expect(crumbs()).toEqual(["Projects", "Kinase Atlas"]);
    expect(within(bar()).getByText("Kinase Atlas")).toHaveAttribute("aria-current", "page");
    expect(within(bar()).getByRole("button", { name: "Add member" })).toBeVisible();
    expect(within(bar()).queryByRole("button", { name: "New" })).toBeNull();
    // The title is the page's, below the bar.
    const title = screen.getByRole("heading", { level: 1, name: "Members" });
    expect(bar().contains(title)).toBe(false);
    expect(screen.getAllByRole("navigation", { name: "Breadcrumb" })).toHaveLength(1);
  });

  it("holds a page's own breadcrumb over its frame's, and its frame's actions if it has none", async () => {
    await renderPages(
      <TopBar
        crumbs={[<a href="/entities/">Entities</a>, "PLS-000001"]}
        actions={<button type="button">Edit</button>}
      />,
      <>
        <PageHeader
          title="Edit pUC19"
          crumbs={[<a href="/entities/">Entities</a>, <a href="/e/">PLS-000001</a>, "Edit"]}
        />
        <span data-testid="drawn" />
      </>,
    );
    expect(crumbs()).toEqual(["Entities", "PLS-000001", "Edit"]);
    expect(within(bar()).getByRole("button", { name: "Edit" })).toBeVisible();
  });

  it("shows the page's title as its breadcrumb when nothing gives one", async () => {
    await renderPages(
      null,
      <>
        <PageHeader title="My work" />
        <span data-testid="drawn" />
      </>,
    );
    expect(bar()).toBeVisible();
    expect(crumbs()).toEqual(["My work"]);
  });

  it("isn't drawn while nothing fills it, and is emptied when its page goes", async () => {
    const router = await renderPages(
      <span data-testid="drawn" />,
      <PageHeader title="Members" actions={<button type="button">Add member</button>} />,
      "/frame",
    );
    expect(bar()).not.toBeVisible();
    act(() => {
      router.history.push("/frame/tab");
    });
    expect(await within(bar()).findByRole("button", { name: "Add member" })).toBeVisible();
    act(() => {
      router.history.push("/frame");
    });
    await waitFor(() => {
      expect(within(bar()).queryByRole("button", { name: "Add member" })).toBeNull();
    });
    expect(bar()).not.toBeVisible();
  });
});

describe("a page's header", () => {
  it("puts what the page is beside its title, and more about it in a toggletip", async () => {
    await renderPages(
      null,
      <>
        <PageHeader
          title="Primers"
          description="Short DNA oligos for PCR and sequencing."
          about={<a href="/types/primer/">About the Primer type</a>}
        />
        <span data-testid="drawn" />
      </>,
    );
    expect(screen.getByText("Short DNA oligos for PCR and sequencing.")).toBeVisible();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "About Primers" }));
    const about = screen.getByRole("dialog", { name: "About Primers" });
    expect(within(about).getByText("Short DNA oligos for PCR and sequencing.")).toBeVisible();
    expect(within(about).getByRole("link", { name: "About the Primer type" })).toHaveAttribute(
      "href",
      "/types/primer/",
    );
  });

  it("offers no toggletip for a description that fits, with nothing more to say", async () => {
    await renderPages(
      null,
      <>
        <PageHeader title="Runs" description="Every run in your projects." />
        <span data-testid="drawn" />
      </>,
    );
    expect(screen.getByText("Every run in your projects.")).toBeVisible();
    expect(screen.queryByRole("button", { name: "About Runs" })).toBeNull();
  });
});

describe("on a phone", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("keeps the breadcrumb in the bar and puts a page's actions under its title", async () => {
    vi.stubGlobal("matchMedia", (query: string) => ({
      matches: true,
      media: query,
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
    }));
    await renderPages(
      null,
      <>
        <PageHeader
          title="Proteins"
          crumbs={[<a href="/entities/">Entities</a>, "Proteins"]}
          actions={<button type="button">New protein</button>}
        />
        <span data-testid="drawn" />
      </>,
    );
    expect(crumbs()).toEqual(["Entities", "Proteins"]);
    expect(within(bar()).queryByRole("button", { name: "New protein" })).toBeNull();
    const title = screen.getByRole("heading", { level: 1, name: "Proteins" });
    const action = screen.getByRole("button", { name: "New protein" });
    // After the title, in the page's order.
    expect(title.compareDocumentPosition(action) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });
});
