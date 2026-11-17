import { QueryObserver } from "@tanstack/react-query";
import { afterEach, expect, test } from "vitest";

import { NOTIFICATIONS, queryClient } from "./api";
import { refresh } from "./events";

afterEach(() => {
  queryClient.clear();
});

/** An inbox list whose loads finish when told to, each answering what existed when it was sent. */
function inbox() {
  const key = [...NOTIFICATIONS, "list"];
  let existing = ["Added you to Structural Biology"];
  const pending: (() => void)[] = [];
  let loads = 0;
  const observer = new QueryObserver(queryClient, {
    queryKey: key,
    queryFn: () => {
      loads += 1;
      const answer = [...existing];
      return new Promise<string[]>((resolve) => {
        pending.push(() => {
          resolve(answer);
        });
      });
    },
  });
  // Subscribed, so the query is active and refreshing fetches it.
  const unsubscribe = observer.subscribe(() => undefined);
  return {
    arrives: (title: string) => {
      existing = [...existing, title];
    },
    /** Finishes every load, including ones started meanwhile, until `until` settles. */
    finish: async (until?: Promise<void>) => {
      let done = until === undefined;
      void until?.then(() => (done = true));
      for (let i = 0; i < 100; i++) {
        while (pending.length) pending.shift()?.();
        await new Promise((resolve) => setTimeout(resolve, 0));
        if (done && !pending.length) return;
      }
      throw new Error("the refresh never finished");
    },
    loads: () => loads,
    shows: () => queryClient.getQueryData<string[]>(key),
    unsubscribe,
  };
}

test("a notification arriving while the inbox is still opening shows once it opens", async () => {
  const list = inbox();
  expect(list.loads()).toBe(1);

  // The first load was sent before this existed; the event mustn't wait on it.
  list.arrives("Added you to Kinase Atlas as Editor");
  await list.finish(refresh());

  expect(list.loads()).toBe(2);
  expect(list.shows()).toEqual([
    "Added you to Structural Biology",
    "Added you to Kinase Atlas as Editor",
  ]);
  list.unsubscribe();
});

test("an open inbox keeps what it shows while it fetches again", async () => {
  const list = inbox();
  await list.finish();
  expect(list.shows()).toEqual(["Added you to Structural Biology"]);

  list.arrives("Added you to Kinase Atlas as Editor");
  const refreshed = refresh();
  expect(list.shows()).toEqual(["Added you to Structural Biology"]);
  await list.finish(refreshed);

  expect(list.shows()).toHaveLength(2);
  list.unsubscribe();
});
