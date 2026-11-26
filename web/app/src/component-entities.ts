/**
 * What fills a component's place (ADR 0117): the entities the server finds for it, a page
 * at a time, as the person viewing it may read them. The frame can ask for more and
 * narrow what it has; it never names the search. The page holds at most the component's
 * limit, makes one request at a time (a new refinement drops the last), and starts again
 * from the first page when the refinement changes.
 */

import { unwrap, type Problem } from "@ikisci/sdk";
import { keepPreviousData, useInfiniteQuery, useQueries } from "@tanstack/react-query";
import { useCallback, useMemo, useState } from "react";

import { iki, problemOf } from "./api";
import {
  onEveryRecord,
  type ComponentEntity,
  type EntityTab,
  type PlaceRequest,
  type PluginComponent,
  type Refinement,
} from "./components";
import { ENTITIES } from "./entities";
import { fetchPageRead } from "./page-reads";

/** The most a request asks for, as the server allows. */
export const PAGE_SIZE = 100;

/** What the element gets as `entities`. */
export interface PlaceEntities {
  items: ComponentEntity[];
  /** There are more it could have, if it asks. */
  more: boolean;
  /** It holds its limit, so it gets no more. */
  limited: boolean;
  loading: boolean;
  /** Why it has none, if the search was refused. */
  error: string | null;
  /** The refinement in force: what it last asked for. */
  refinement: Refinement | null;
}

export interface PlaceData {
  entities: PlaceEntities;
  /** Why the search was refused, for the page to say. */
  problem: Problem | null;
  /** Whether the first page has come (or couldn't). */
  settled: boolean;
  more: () => void;
  refine: (refinement: Refinement | null) => void;
}

/** The entities that fill `place` for `component`; nothing is asked for while `place` is
 * null, or if the component shows only one entity. */
export function usePlaceEntities(
  component: PluginComponent,
  place: PlaceRequest | null,
): PlaceData {
  const [refinement, setRefinement] = useState<Refinement | null>(null);
  const limit = component.entities?.limit ?? 0;
  const size = Math.min(PAGE_SIZE, limit);
  const query = useInfiniteQuery({
    queryKey: [...ENTITIES, "component", component.name, place, refinement],
    enabled: place !== null && component.entities !== null,
    initialPageParam: undefined as string | undefined,
    queryFn: async ({ pageParam, signal }) =>
      unwrap(
        await iki.POST("/api/v1/components/{name}/entities", {
          fetch: fetchPageRead,
          params: { path: { name: component.name } },
          body: {
            home: false,
            ...(place ?? { home: true }),
            ...(refinement ? { refine: refinement } : {}),
            limit: size,
            ...(pageParam ? { cursor: pageParam } : {}),
          },
          // A newer refinement or page drops this one: one request at a time.
          signal: signal,
        }),
      ),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    // A refused search won't be accepted by asking again.
    retry: false,
    // What it had stays while a refinement's first page comes, so it doesn't flicker.
    placeholderData: keepPreviousData,
  });
  const { data, error, hasNextPage, isFetching, isFetchingNextPage, fetchNextPage } = query;
  const problem = useMemo(() => (error ? problemOf(error) : null), [error]);
  // The same object until something in it changes, so the frame is sent it only then.
  const entities = useMemo<PlaceEntities>(() => {
    const all = data?.pages.flatMap((p) => p.items) ?? [];
    const limited = all.length >= limit && (hasNextPage || all.length > limit);
    return {
      items: all.slice(0, limit),
      more: hasNextPage && !limited,
      limited,
      loading: isFetching,
      error: problem?.detail ?? null,
      refinement,
    };
  }, [data, limit, hasNextPage, isFetching, problem, refinement]);
  const { limited } = entities;
  const more = useCallback(() => {
    if (hasNextPage && !isFetchingNextPage && !limited) void fetchNextPage();
  }, [hasNextPage, isFetchingNextPage, limited, fetchNextPage]);
  const refine = useCallback((next: Refinement | null) => {
    setRefinement((now) => (JSON.stringify(now) === JSON.stringify(next) ? now : next));
  }, []);
  return {
    entities,
    problem,
    settled: query.isSuccess || query.isError,
    more,
    refine,
  };
}

/** The entity's tabs, less those on every record whose search finds nothing for it (ADR
 * 0237), and whether that's known yet. Each asks for one entity, as the person may read
 * it. A tab whose search is refused stays, so its page can say why. */
export function useFoundTabs(
  entity: string,
  tabs: EntityTab[],
): { tabs: EntityTab[]; settled: boolean } {
  const asked = tabs.filter(onEveryRecord);
  const found = useQueries({
    queries: asked.map((tab) => ({
      queryKey: [...ENTITIES, "component", tab.component.name, { entity }, "found"],
      queryFn: async ({ signal }: { signal: AbortSignal }) =>
        unwrap(
          await iki.POST("/api/v1/components/{name}/entities", {
            fetch: fetchPageRead,
            params: { path: { name: tab.component.name } },
            body: { home: false, entity, limit: 1 },
            signal,
          }),
        ).items.length > 0,
      retry: false,
    })),
  });
  const empty = new Set(
    asked.filter((_, i) => found[i]?.data === false).map((tab) => tab.component.name),
  );
  return {
    tabs: tabs.filter((tab) => !empty.has(tab.component.name)),
    settled: found.every((q) => !q.isLoading),
  };
}
