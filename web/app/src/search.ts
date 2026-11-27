/**
 * Searching everything (ADR 0176): what the palette asks the server, how each result
 * opens, and the ⌘K shortcut that opens it, on the app's pages and the server's.
 *
 * Anything that opens the palette dispatches `iki:search` on the window: the sidebar's
 * button, the shortcut here, a page's own link. The palette listens for it.
 */

import { unwrap, type Schemas } from "@ikisci/sdk";
import { defaultStringifySearch } from "@tanstack/react-router";
import { keepPreviousData, queryOptions } from "@tanstack/react-query";

import { iki } from "./api";
import { toAddress } from "./search-address";

export type SearchAnswer = Schemas["SearchEverythingOut"];
export type SearchGroup = Schemas["SearchGroupOut"];
export type SearchResult = Schemas["SearchResultOut"];
export type SearchKind = SearchGroup["kind"];

/** The event that opens the palette. */
export const SEARCH_EVENT = "iki:search";

/** The shortest text the server searches records, requests and runs for. */
export const MIN_TEXT = 3;

/** How long typing pauses before the palette asks: one request a word, not a letter. */
export const DEBOUNCE_MS = 150;

export const KIND_LABELS: Record<SearchKind, string> = {
  entities: "Entities",
  requests: "Requests",
  runs: "Runs",
  files: "Files",
  projects: "Projects",
  saved_views: "Saved views",
  people: "People",
};

/** What a result is, said beside it: "Request", "Run", "Saved view". */
export const RESULT_LABELS: Record<SearchResult["kind"], string> = {
  entity: "Entity",
  request: "Request",
  run: "Run",
  file: "File",
  project: "Project",
  saved_view: "Saved view",
  person: "Person",
};

/** Opens the palette, wherever it's listening. */
export function openSearch(): void {
  window.dispatchEvent(new CustomEvent(SEARCH_EVENT));
}

/** Whether a key press is ⌘K (or Ctrl+K off the Mac): the palette's shortcut. */
export function isSearchShortcut(event: KeyboardEvent): boolean {
  return (
    event.key.toLowerCase() === "k" &&
    (event.metaKey || event.ctrlKey) &&
    !event.altKey &&
    !event.shiftKey
  );
}

const BOUND = Symbol.for("iki.searchShortcut");

/**
 * Makes ⌘K (Ctrl+K) open the palette, once per page however often it's called. A press
 * something else already took (its default prevented) is left alone, so a second
 * listener for the same shortcut, such as the sidebar's, can't open it twice.
 */
export function bindSearchShortcut(target: Window = window): void {
  const marked = target as unknown as Record<symbol, boolean>;
  if (marked[BOUND]) return;
  marked[BOUND] = true;
  target.addEventListener("keydown", (event) => {
    if (event.defaultPrevented || !isSearchShortcut(event)) return;
    event.preventDefault();
    openSearch();
  });
}

export function searchQuery(q: string, kind: SearchKind | null) {
  return queryOptions({
    queryKey: ["search", q, kind],
    queryFn: async ({ signal }) =>
      unwrap(
        await iki.GET("/api/v1/search", {
          params: { query: kind ? { q, kind } : { q } },
          signal,
        }),
      ),
    // What was found for the last text stays while the next is found: nothing jumps.
    placeholderData: keepPreviousData,
    staleTime: 15_000,
  });
}

/**
 * Where a result opens. A saved view opens in its project's grid as its saved searches
 * do, searching as it was saved; one the grid can't show (made with the API) opens the
 * grid itself.
 */
export function resultHref(result: SearchResult): string {
  if (result.kind !== "saved_view" || !result.query) return result.href;
  const address = toAddress(result.query);
  if (!address) return result.href;
  const search = defaultStringifySearch(address);
  return `${result.href}${search}`;
}

/** The paths the web app draws itself: opening one there needs no page load. */
const APP_PATHS = ["/entities/", "/requests/", "/runs/", "/projects/", "/work/", "/types/"];

export function isAppPath(href: string): boolean {
  return APP_PATHS.some((p) => href.startsWith(p));
}

/** A group's count as shown: "12", or "1,000+" past what the server counts to. */
export function shownTotal(group: SearchGroup): string {
  if (group.total === null) return "";
  const n = group.total.toLocaleString("en-US");
  return group.more_than_total ? `${n}+` : n;
}
