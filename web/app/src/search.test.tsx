/**
 * Search everything (ADR 0176) against a fake API: the ⌘K palette opens on `iki:search`
 * and the shortcut, asks as typing pauses, shows each kind's results with their projects
 * and links to the full lists, and is driven from the keyboard as a combobox.
 */

import { QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import { queryClient } from "./api";
import { swatchOf } from "./project-color";
import {
  bindSearchShortcut,
  isSearchShortcut,
  openSearch,
  SEARCH_EVENT,
  type SearchAnswer,
  type SearchGroup,
  type SearchResult,
} from "./search";
import { SearchPalette } from "./search-palette";
import { answer, calls } from "./test-server";
import { typeSwatch } from "./type-color";

const KINASE = {
  id: "00000000-0000-7000-8000-0000000000p1",
  name: "Kinase Atlas",
  slug: "kinase-atlas",
};
const ENZYMES = { id: "00000000-0000-7000-8000-0000000000p2", name: "Enzymes", slug: "enzymes" };

function result(overrides: Partial<SearchResult>): SearchResult {
  return {
    kind: "entity",
    id: "00000000-0000-7000-8000-00000000e001",
    hrid: "PRT-000013",
    title: "Lysozyme C",
    detail: "Protein",
    type: "acme/protein.Protein",
    projects: [KINASE],
    href: "/entities/PRT-000013/?project=kinase-atlas",
    query: null,
    ...overrides,
  };
}

function group(kind: SearchGroup["kind"], items: SearchResult[], extra: Partial<SearchGroup> = {}) {
  const labels = {
    entities: "Entities",
    requests: "Requests",
    runs: "Runs",
    files: "Files",
    projects: "Projects",
    saved_views: "Saved views",
    people: "People",
  };
  return {
    kind,
    label: labels[kind],
    total: items.length,
    more_than_total: false,
    href: null,
    items,
    ...extra,
  } satisfies SearchGroup;
}

const LYSOZYME = result({});
const LYSOZYME_B = result({
  id: "00000000-0000-7000-8000-00000000e002",
  hrid: "PRT-000014",
  title: "Lysozyme B",
  projects: [ENZYMES, KINASE],
  href: "/entities/PRT-000014/?project=enzymes",
});
const REQUEST = result({
  kind: "request",
  id: "00000000-0000-7000-8000-00000000e003",
  hrid: "ARQ-000004",
  title: "Lysozyme purity",
  detail: "Assay request",
  href: "/entities/ARQ-000004/",
});
const SAVED = result({
  kind: "saved_view",
  id: "00000000-0000-7000-8000-00000000e004",
  hrid: null,
  title: "Lysozyme work",
  detail: "Shared with the project",
  type: null,
  href: "/saved-searches/00000000-0000-7000-8000-00000000e004/",
  query: { type: "acme/protein.Protein", q: "lysozyme" },
});

function found(q: string, groups: SearchGroup[], extra: Partial<SearchAnswer> = {}): SearchAnswer {
  return { q, groups, searched_records: true, recent: [], projects_readable: 2, ...extra };
}

const LYS = found("lys", [
  group("entities", [LYSOZYME_B, LYSOZYME], { total: 23, href: "/entities/?q=lys" }),
  group("requests", [REQUEST], { href: "/requests/?q=lys" }),
  group("runs", [], { href: "/runs/?q=lys" }),
  group("projects", [], { href: "/projects/" }),
  group("saved_views", [SAVED]),
  group("people", []),
]);

const RECENT = found("", [], {
  searched_records: false,
  recent: [
    result({ kind: "run", hrid: "RUN-000031", title: "Compute properties", detail: "Succeeded" }),
  ],
});

/** Answers the palette's searches. An answer `held` for some text (or `kind=…`) waits until
 * the test lets it go, as a slow server's would. */
function answerSearch(held = new Map<string, Promise<void>>()) {
  answer("GET /api/v1/search", async (sent: { query: URLSearchParams }) => {
    const q = sent.query.get("q") ?? "";
    await (held.get(q) ?? held.get(`kind=${sent.query.get("kind") ?? ""}`));
    if (q === "") return RECENT;
    if (sent.query.get("kind") === "requests") {
      return found(q, [group("requests", [REQUEST], { href: "/requests/?q=lys" })]);
    }
    if (q === "lysozyme b") {
      return found(q, [group("entities", [LYSOZYME_B], { href: "/entities/?q=lysozyme+b" })]);
    }
    if (q === "ly") {
      return found("ly", [group("projects", []), group("saved_views", [SAVED])], {
        searched_records: false,
      });
    }
    if (q === "nothing")
      return found(
        q,
        LYS.groups.map((g) => ({ ...g, total: 0, items: [] })),
      );
    return { ...LYS, q };
  });
}

function renderPalette() {
  const navigate = vi.fn<(href: string) => void>();
  render(
    <QueryClientProvider client={queryClient}>
      <button type="button" onClick={openSearch}>
        Search everything
      </button>
      <SearchPalette navigate={navigate} />
    </QueryClientProvider>,
  );
  return { navigate, user: userEvent.setup() };
}

async function opened() {
  return screen.findByRole("combobox", { name: "Search everything you can read" });
}

beforeAll(() => {
  // jsdom lays nothing out, so it has nothing to scroll into view.
  Element.prototype.scrollIntoView = () => undefined;
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("search everything", () => {
  it("opens on iki:search, focused, and shows what you changed or ran lately", async () => {
    answerSearch();
    const { user } = renderPalette();
    await user.click(screen.getByRole("button", { name: "Search everything" }));
    const box = await opened();
    expect(box).toHaveFocus();
    expect(screen.getByRole("dialog", { name: "Search everything" })).toBeInTheDocument();
    const recent = await screen.findByRole("group", { name: "Recently changed or run by you" });
    expect(within(recent).getByRole("option", { name: /RUN-000031/ })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getByText(/never appear, not even as a count/)).toBeInTheDocument();
  });

  it("asks once typing pauses, and groups what it finds by kind with their projects", async () => {
    answerSearch();
    const { user } = renderPalette();
    act(openSearch);
    await user.type(await opened(), "lys");
    const entities = await screen.findByRole("group", { name: "Entities" });
    // One request for the word, not one a letter.
    expect(calls("GET", "/api/v1/search").map((c) => c.query.get("q"))).toEqual(["", "lys"]);
    const options = within(entities).getAllByRole("option");
    expect(options.map((o) => o.textContent)).toEqual([
      expect.stringContaining("PRT-000014Lysozyme BProteinEnzymes +1"),
      expect.stringContaining("PRT-000013Lysozyme CProteinKinase Atlas"),
      "All 23 in Entities →",
    ]);
    // A record in two projects names both, for those who can't see the "+1".
    expect(options[0]).toHaveTextContent(", and Kinase Atlas");
    // Each record's dot is its type's color.
    expect(options[0]?.querySelector(".type-dot")).toHaveAttribute(
      "data-swatch",
      String(typeSwatch("acme/protein.Protein")),
    );
    expect(options[2]).toHaveAttribute("href", "/entities/?q=lys");
    expect(screen.getByRole("group", { name: "Requests" })).toBeInTheDocument();
    // Kinds with nothing found aren't shown, nor their pills.
    expect(screen.queryByRole("group", { name: "Runs" })).not.toBeInTheDocument();
    const kinds = screen.getByRole("group", { name: "Kind" });
    expect(
      within(kinds)
        .getAllByRole("button")
        .map((b) => b.textContent),
    ).toEqual(["All25", "Entities23", "Requests1", "Saved views1"]);
    expect(screen.getByText("In all 2 projects you can read")).toBeInTheDocument();
  });

  it("marks each result's project with that project's own swatch", async () => {
    answerSearch();
    const { user } = renderPalette();
    act(openSearch);
    await user.type(await opened(), "lys");
    const entities = await screen.findByRole("group", { name: "Entities" });
    const marks = within(entities)
      .getAllByRole("option")
      .map((o) => o.querySelector(".palette-project-mark")?.getAttribute("data-swatch"));
    // The first project shown is the owner's: Enzymes, then Kinase Atlas.
    expect(marks.slice(0, 2)).toEqual([String(swatchOf(ENZYMES.id)), String(swatchOf(KINASE.id))]);
  });

  it("moves with ↑ and ↓ as a combobox, and opens with ↵", async () => {
    answerSearch();
    const { user, navigate } = renderPalette();
    act(openSearch);
    const box = await opened();
    await user.type(box, "lys");
    await screen.findByRole("group", { name: "Entities" });
    const listbox = screen.getByRole("listbox", { name: "Results for lys" });
    expect(box).toHaveAttribute("aria-controls", listbox.id);
    expect(box).toHaveAttribute("aria-expanded", "true");
    const first = within(listbox).getAllByRole("option")[0];
    expect(box).toHaveAttribute("aria-activedescendant", first?.id);
    await user.keyboard("{ArrowDown}{ArrowDown}");
    const all = within(listbox).getByRole("option", { name: "All 23 in Entities" });
    expect(box).toHaveAttribute("aria-activedescendant", all.id);
    expect(all).toHaveAttribute("aria-selected", "true");
    // Up from the first goes round to the last.
    await user.keyboard("{ArrowUp}{ArrowUp}{ArrowUp}");
    const last = within(listbox).getAllByRole("option").at(-1);
    expect(box).toHaveAttribute("aria-activedescendant", last?.id);
    await user.keyboard("{ArrowDown}{Enter}");
    expect(navigate).toHaveBeenCalledWith("/entities/PRT-000014/?project=enzymes");
    // Opening it closes the palette.
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
  });

  it("opens in a new tab with ⌘↵ or Ctrl+↵, and stays open", async () => {
    answerSearch();
    const opening = vi.spyOn(window, "open").mockReturnValue(null);
    const { user, navigate } = renderPalette();
    act(openSearch);
    await user.type(await opened(), "lys");
    await screen.findByRole("group", { name: "Entities" });
    await user.keyboard("{Meta>}{Enter}{/Meta}");
    await user.keyboard("{ArrowDown}{Control>}{Enter}{/Control}");
    expect(opening.mock.calls).toEqual([
      ["/entities/PRT-000014/?project=enzymes", "_blank", "noopener"],
      ["/entities/PRT-000013/?project=kinase-atlas", "_blank", "noopener"],
    ]);
    expect(navigate).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("opens nothing of the last text's answer when ↵ comes straight after new text", async () => {
    answerSearch();
    const opening = vi.spyOn(window, "open").mockReturnValue(null);
    const { user, navigate } = renderPalette();
    act(openSearch);
    const box = await opened();
    await user.type(box, "lys");
    await screen.findByRole("group", { name: "Entities" });
    await user.clear(box);
    await user.paste("nothing");
    // Lysozyme B is still shown, from the answer to "lys", but isn't what was typed.
    expect(screen.getByRole("option", { name: /PRT-000014/ })).toBeInTheDocument();
    expect(box).not.toHaveAttribute("aria-activedescendant");
    expect(screen.getByRole("listbox")).toHaveAttribute("aria-busy", "true");
    await user.keyboard("{Enter}{Meta>}{Enter}{/Meta}{ArrowDown}");
    expect(await screen.findByText("Nothing you can read matches “nothing”.")).toBeInTheDocument();
    expect(navigate).not.toHaveBeenCalled();
    expect(opening).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("opens the first of what was typed when ↵ comes before its answer", async () => {
    let release: () => void = () => undefined;
    const slow = new Promise<void>((resolve) => {
      release = resolve;
    });
    answerSearch(new Map([["lysozyme b", slow]]));
    const { user, navigate } = renderPalette();
    act(openSearch);
    const box = await opened();
    await user.type(box, "lys");
    await screen.findByRole("group", { name: "Entities" });
    await user.clear(box);
    await user.paste("lysozyme b");
    await user.keyboard("{Enter}");
    await waitFor(() => {
      expect(calls("GET", "/api/v1/search").at(-1)?.query.get("q")).toBe("lysozyme b");
    });
    // Asked, but not answered: the last answer's first, Lysozyme B, waits too.
    expect(screen.getByRole("listbox")).toHaveAttribute("aria-busy", "true");
    expect(navigate).not.toHaveBeenCalled();
    act(release);
    await waitFor(() => {
      expect(navigate).toHaveBeenCalledWith("/entities/PRT-000014/?project=enzymes");
    });
    expect(navigate).toHaveBeenCalledTimes(1);
  });

  it("opens the kind chosen's first when ↵ comes before its answer", async () => {
    let release: () => void = () => undefined;
    const slow = new Promise<void>((resolve) => {
      release = resolve;
    });
    answerSearch(new Map([["kind=requests", slow]]));
    const { user, navigate } = renderPalette();
    act(openSearch);
    const box = await opened();
    await user.type(box, "lys");
    await screen.findByRole("group", { name: "Entities" });
    await user.click(screen.getByRole("button", { name: "Requests1" }));
    await user.keyboard("{Enter}");
    // The entities still shown are the answer for every kind, not for requests.
    expect(screen.getByRole("group", { name: "Entities" })).toBeInTheDocument();
    expect(navigate).not.toHaveBeenCalled();
    act(release);
    await waitFor(() => {
      expect(navigate).toHaveBeenCalledWith("/entities/ARQ-000004/");
    });
    expect(navigate).toHaveBeenCalledTimes(1);
  });

  it("forgets a waiting ↵ once the text changes again", async () => {
    let release: () => void = () => undefined;
    const slow = new Promise<void>((resolve) => {
      release = resolve;
    });
    answerSearch(new Map([["lysozyme b", slow]]));
    const { user, navigate } = renderPalette();
    act(openSearch);
    const box = await opened();
    await user.type(box, "lysozyme b");
    await user.keyboard("{Enter}");
    await waitFor(() => {
      expect(calls("GET", "/api/v1/search").at(-1)?.query.get("q")).toBe("lysozyme b");
    });
    await user.type(box, "x");
    act(release);
    const listbox = await screen.findByRole("listbox", { name: "Results for lysozyme bx" });
    await waitFor(() => {
      expect(listbox).toHaveAttribute("aria-busy", "false");
    });
    expect(screen.getAllByRole("option").length).toBeGreaterThan(0);
    expect(navigate).not.toHaveBeenCalled();
  });

  it("narrows to one kind with its pill, asking for that kind alone", async () => {
    answerSearch();
    const { user } = renderPalette();
    act(openSearch);
    await user.type(await opened(), "lys");
    await screen.findByRole("group", { name: "Entities" });
    await user.click(screen.getByRole("button", { name: "Requests1" }));
    await waitFor(() => {
      expect(screen.queryByRole("group", { name: "Entities" })).not.toBeInTheDocument();
    });
    expect(calls("GET", "/api/v1/search").at(-1)?.query.get("kind")).toBe("requests");
    expect(screen.getByRole("button", { name: "Requests1" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    // The other kinds' counts stay, to go back to.
    expect(screen.getByRole("button", { name: "Entities23" })).toBeInTheDocument();
    expect(screen.getByRole("combobox")).toHaveFocus();
  });

  it("says what short text searches, and when nothing matches", async () => {
    answerSearch();
    const { user } = renderPalette();
    act(openSearch);
    const box = await opened();
    await user.type(box, "ly");
    expect(await screen.findByText(/Type 3 or more characters/)).toBeInTheDocument();
    expect(screen.getByRole("group", { name: "Saved views" })).toBeInTheDocument();
    await user.clear(box);
    await user.type(box, "nothing");
    expect(await screen.findByText("Nothing you can read matches “nothing”.")).toBeInTheDocument();
    expect(box).toHaveAttribute("aria-expanded", "false");
    expect(box).not.toHaveAttribute("aria-activedescendant");
  });

  it("closes with Esc, putting focus back where it was", async () => {
    answerSearch();
    const { user } = renderPalette();
    const trigger = screen.getByRole("button", { name: "Search everything" });
    await user.click(trigger);
    await opened();
    await user.keyboard("{Escape}");
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    await waitFor(() => {
      expect(trigger).toHaveFocus();
    });
  });

  it("opens a result clicked, and leaves a modified click to the browser", async () => {
    answerSearch();
    const { user, navigate } = renderPalette();
    act(openSearch);
    await user.type(await opened(), "lys");
    const request = await screen.findByRole("option", { name: /ARQ-000004/ });
    expect(request).toHaveAttribute("href", "/entities/ARQ-000004/");
    await user.click(request);
    expect(navigate).toHaveBeenCalledWith("/entities/ARQ-000004/");
  });
});

describe("the shortcut", () => {
  it("is ⌘K or Ctrl+K, and only those", () => {
    const press = (init: KeyboardEventInit) => isSearchShortcut(new KeyboardEvent("keydown", init));
    expect(press({ key: "k", metaKey: true })).toBe(true);
    expect(press({ key: "K", ctrlKey: true })).toBe(true);
    expect(press({ key: "k" })).toBe(false);
    expect(press({ key: "k", metaKey: true, shiftKey: true })).toBe(false);
    expect(press({ key: "j", metaKey: true })).toBe(false);
  });

  it("is bound once, however often it's asked, and leaves a press already taken", () => {
    const heard = vi.fn();
    window.addEventListener(SEARCH_EVENT, heard);
    bindSearchShortcut();
    bindSearchShortcut();
    window.dispatchEvent(
      new KeyboardEvent("keydown", { key: "k", ctrlKey: true, cancelable: true }),
    );
    expect(heard).toHaveBeenCalledTimes(1);
    const taken = new KeyboardEvent("keydown", { key: "k", metaKey: true, cancelable: true });
    taken.preventDefault();
    window.dispatchEvent(taken);
    expect(heard).toHaveBeenCalledTimes(1);
    window.removeEventListener(SEARCH_EVENT, heard);
  });

  it("opens the palette from anywhere on the page", async () => {
    answerSearch();
    const { user } = renderPalette();
    await user.keyboard("{Control>}k{/Control}");
    expect(await opened()).toHaveFocus();
  });
});

describe("where results open", () => {
  it("opens a saved view from its own address, as the sidebar does", async () => {
    // Exact type alone, and either of two conditions: searches the grid can't show. Its
    // address says so, rather than opening the grid without them.
    const unshown = result({
      ...SAVED,
      id: "00000000-0000-7000-8000-00000000e005",
      title: "Lysozymes, this type only",
      href: "/saved-searches/00000000-0000-7000-8000-00000000e005/",
      query: { exact_type: true, type: "acme/protein.Protein" },
    });
    const either = result({
      ...SAVED,
      id: "00000000-0000-7000-8000-00000000e006",
      title: "Lysozymes, either assay",
      href: "/saved-searches/00000000-0000-7000-8000-00000000e006/",
      query: {
        where: {
          any: [
            { field: "assay", op: "eq", value: "purity" },
            { field: "assay", op: "eq", value: "activity" },
          ],
        },
      },
    });
    answer("GET /api/v1/search", (sent: { query: URLSearchParams }) => {
      const q = sent.query.get("q") ?? "";
      return q ? found(q, [group("saved_views", [SAVED, unshown, either])]) : RECENT;
    });
    const { user, navigate } = renderPalette();
    act(openSearch);
    await user.type(await opened(), "lysozyme");
    const views = await screen.findByRole("group", { name: "Saved views" });
    await waitFor(() => {
      expect(screen.getByRole("listbox")).toHaveAttribute("aria-busy", "false");
    });
    expect(
      within(views)
        .getAllByRole("option")
        .map((o) => o.getAttribute("href")),
    ).toEqual([SAVED.href, unshown.href, either.href]);
    await user.keyboard("{ArrowDown}{Enter}");
    expect(navigate).toHaveBeenCalledWith("/saved-searches/00000000-0000-7000-8000-00000000e005/");
  });
});
