/**
 * Mutations call back once they end, even when the component that started one
 * has gone: what they refresh often replaces it.
 */

import { ApiError } from "@ikisci/sdk";
import { QueryClientProvider, QueryObserver } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import {
  chooseWatch,
  iki,
  markAllNotifications,
  projectPagesQuery,
  projectsQuery,
  queryClient,
  rolesQuery,
  useApiMutation,
  watchQuery,
} from "./api";
import { entitiesQuery } from "./entities";
import { intakeQuery, ownsRequestsQuery, queuedQuery, sentToQuery } from "./requests";
import { answer, calls } from "./test-server";

function Starter({
  run,
  onSuccess,
  onError,
}: {
  run: () => Promise<string>;
  onSuccess: (result: string) => void;
  onError: (error: ApiError) => void;
}) {
  const mutation = useApiMutation(run, []);
  return (
    <button
      type="button"
      onClick={() => {
        mutation.mutate(undefined, { onSuccess, onError });
      }}
    >
      Start
    </button>
  );
}

function Page(props: Parameters<typeof Starter>[0]) {
  const [shown, setShown] = useState(true);
  return (
    <QueryClientProvider client={queryClient}>
      {shown ? <Starter {...props} /> : null}
      <button
        type="button"
        onClick={() => {
          setShown(false);
        }}
      >
        Hide
      </button>
    </QueryClientProvider>
  );
}

function later<T>(): {
  promise: Promise<T>;
  resolve: (v: T) => void;
  reject: (e: unknown) => void;
} {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}

describe("useApiMutation", () => {
  it("calls back after the component that started it is gone", async () => {
    const pending = later<string>();
    const onSuccess = vi.fn();
    render(<Page run={() => pending.promise} onSuccess={onSuccess} onError={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: "Start" }));
    await userEvent.click(screen.getByRole("button", { name: "Hide" }));
    expect(screen.queryByRole("button", { name: "Start" })).not.toBeInTheDocument();
    pending.resolve("done");
    await waitFor(() => {
      expect(onSuccess).toHaveBeenCalledWith("done", undefined);
    });
  });

  it("passes on the API's refusals, and nothing else", async () => {
    const refused = later<string>();
    const onError = vi.fn();
    render(<Page run={() => refused.promise} onSuccess={vi.fn()} onError={onError} />);
    await userEvent.click(screen.getByRole("button", { name: "Start" }));
    const problem = { status: 409, title: "Conflict", detail: "Already decided." };
    refused.reject(new ApiError(problem as ConstructorParameters<typeof ApiError>[0]));
    await waitFor(() => {
      expect(onError).toHaveBeenCalledTimes(1);
    });
    expect((onError.mock.calls[0]?.[0] as ApiError).problem.detail).toBe("Already decided.");
  });
});

describe("queries belonging to the current page", () => {
  function transition(name: "pagehide" | "pageshow") {
    window.dispatchEvent(new PageTransitionEvent(name, { persisted: true }));
  }

  it("waits for a restored page before starting its dependent reads, and still sends a chosen write", async () => {
    const watch = "/api/v1/entities/TUB-1/watch";
    const search = "/api/v1/entities/search";
    const intake = "/api/v1/projects/p/intake";
    const sentTo = "/api/v1/projects/p/sent-to";
    const projects = "/api/v1/projects";
    answer(`GET ${watch}`, { mode: "watching" });
    answer(`PUT ${watch}`, { mode: "watching" });
    answer(`GET ${intake}`, { types: [] });
    answer(`GET ${sentTo}`, []);
    answer(`POST ${search}`, { items: [] });
    answer(`GET ${projects}`, { items: [] });
    transition("pagehide");
    const reads = Promise.all([
      queryClient.query(watchQuery("TUB-1")),
      queryClient.query(intakeQuery("p")),
      queryClient.query(queuedQuery("p")),
      queryClient.query(sentToQuery("p")),
      queryClient.query(ownsRequestsQuery("p", ["Request"])),
      queryClient.infiniteQuery(projectPagesQuery({})),
    ]);
    try {
      await chooseWatch("TUB-1", "watching");
      expect(calls("PUT", watch)).toHaveLength(1);
      expect(calls("GET")).toEqual([]);
      expect(calls("POST")).toEqual([]);
    } finally {
      transition("pageshow");
    }
    await reads;
    expect(
      calls("GET")
        .map((request) => request.path)
        .sort(),
    ).toEqual([watch, intake, sentTo, projects].sort());
    expect(calls("POST", search)).toHaveLength(2);
  });

  it("aborts an outstanding watch read and finishes it once the same document returns", async () => {
    const watch = "/api/v1/entities/TUB-1/watch";
    const first = later<Request>();
    answer(`GET ${watch}`, { mode: "watching" });
    const transport = vi.spyOn(globalThis, "fetch").mockImplementationOnce((input, init) => {
      const request = new Request(input, init);
      first.resolve(request);
      return new Promise((_resolve, reject) => {
        request.signal.addEventListener("abort", () => {
          reject(request.signal.reason as Error);
        });
      });
    });
    try {
      const result = queryClient.query(watchQuery("TUB-1"));
      const pending = await first.promise;
      transition("pagehide");
      try {
        expect(pending.signal.aborted).toBe(true);
        expect(transport).toHaveBeenCalledTimes(1);
      } finally {
        transition("pageshow");
      }
      await expect(result).resolves.toMatchObject({ mode: "watching" });
      expect(transport).toHaveBeenCalledTimes(2);
    } finally {
      transport.mockRestore();
    }
  });

  it("leaves a genuine live failure visible even if navigation follows", async () => {
    const failure = new TypeError("Failed to fetch");
    const response = later<Response>();
    const sent = later<boolean>();
    const transport = vi.spyOn(globalThis, "fetch").mockImplementationOnce(() => {
      sent.resolve(true);
      return response.promise;
    });
    try {
      const result = queryClient.query({ ...watchQuery("TUB-1"), retry: false });
      await sent.promise;
      response.reject(failure);
      transition("pagehide");
      try {
        await expect(result).rejects.toBe(failure);
      } finally {
        transition("pageshow");
      }
      expect(transport).toHaveBeenCalledTimes(1);
    } finally {
      transport.mockRestore();
    }
  });

  it.each([
    {
      method: "GET",
      path: "/api/v1/project-roles",
      response: [],
      read: () => queryClient.query(rolesQuery),
    },
    {
      method: "POST",
      path: "/api/v1/entities/search",
      response: { items: [] },
      read: () => queryClient.infiniteQuery(entitiesQuery({})),
    },
  ])(
    "stops a $method read if the document left during SDK middleware",
    async ({ method, path, response, read }) => {
      const entered = later<boolean>();
      const release = later<boolean>();
      answer(`${method} ${path}`, response);
      const middleware: Parameters<typeof iki.use>[0] = {
        async onRequest({ request }) {
          if (new URL(request.url).pathname === path) {
            entered.resolve(true);
            await release.promise;
          }
          return request;
        },
      };
      iki.use(middleware);
      try {
        const result = read();
        await entered.promise;
        transition("pagehide");
        try {
          release.resolve(true);
          answer("PUT /api/v1/entities/TUB-1/watch", { mode: "watching" });
          await chooseWatch("TUB-1", "watching");
          expect(calls(method, path)).toHaveLength(0);
        } finally {
          transition("pageshow");
        }
        await result;
        expect(calls(method, path)).toHaveLength(1);
      } finally {
        iki.eject(middleware);
      }
    },
  );

  it("drops a deferred read if its last observer has gone before restoration", async () => {
    transition("pagehide");
    try {
      const observer = new QueryObserver(queryClient, watchQuery("TUB-1"));
      const unsubscribe = observer.subscribe(() => undefined);
      unsubscribe();
    } finally {
      transition("pageshow");
    }
    const current = "/api/v1/entities/TUB-2/watch";
    answer(`GET ${current}`, { mode: "default" });
    await queryClient.query(watchQuery("TUB-2"));
    expect(calls("GET").map((request) => request.path)).toEqual([current]);
  });

  /** The browser's transport: each request held until the test answers it, or rejected with
   * its signal's reason once that aborts, as fetch does. */
  function holding() {
    const sent: { request: Request; answer: (body: unknown) => void }[] = [];
    const spy = vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const request = new Request(input, init);
      return new Promise<Response>((resolve, reject) => {
        sent.push({
          request,
          answer: (body) => {
            resolve(Response.json(body));
          },
        });
        request.signal.addEventListener("abort", () => {
          reject(request.signal.reason as Error);
        });
      });
    });
    const paths = () => sent.map(({ request }) => new URL(request.url).search);
    return { sent, spy, paths };
  }

  /** Leaving: as a navigation begins, then once the page has gone. */
  const leaving = {
    beforeunload: () => window.dispatchEvent(new Event("beforeunload", { cancelable: true })),
    pagehide: () => {
      transition("pagehide");
    },
  };

  it.each([
    {
      write: "PUT",
      save: () => chooseWatch("TUB-1", "muted"),
      answer: { mode: "muted" },
    },
    {
      write: "POST",
      save: () => markAllNotifications("2026-11-23T12:00:00Z"),
      answer: { marked: 3 },
    },
  ])("lets a $write already sent finish though the page goes away", async (write) => {
    for (const leave of Object.values(leaving)) {
      const { sent, spy } = holding();
      try {
        const saving = write.save();
        await vi.waitFor(() => {
          expect(sent).toHaveLength(1);
        });
        const save = sent[0];
        expect(save?.request.method).toBe(write.write);
        leave();
        try {
          expect(save?.request.signal.aborted).toBe(false);
          save?.answer(write.answer);
          await expect(saving).resolves.toEqual(write.answer);
        } finally {
          transition("pagehide");
          transition("pageshow");
        }
      } finally {
        spy.mockRestore();
      }
    }
  });

  it.each(Object.keys(leaving) as (keyof typeof leaving)[])(
    "stops a search, a read that POSTs, at %s, quietly",
    async (event) => {
      const { sent, spy } = holding();
      const failed = vi.spyOn(console, "error");
      try {
        const found = queryClient.infiniteQuery(entitiesQuery({}));
        await vi.waitFor(() => {
          expect(sent).toHaveLength(1);
        });
        const search = sent[0];
        expect(search?.request.method).toBe("POST");
        leaving[event]();
        try {
          expect(search?.request.signal.aborted).toBe(true);
          await new Promise((resolve) => setTimeout(resolve, 50));
          const state = queryClient.getQueryState(entitiesQuery({}).queryKey);
          expect(state?.status).toBe("pending");
          expect(state?.error).toBeNull();
          expect(sent).toHaveLength(1);
        } finally {
          transition("pagehide");
          transition("pageshow");
        }
        await vi.waitFor(() => {
          expect(sent).toHaveLength(2);
        });
        sent[1]?.answer({ items: [], next_cursor: null });
        await expect(found).resolves.toMatchObject({ pages: [{ items: [] }] });
        expect(failed).not.toHaveBeenCalled();
      } finally {
        spy.mockRestore();
        failed.mockRestore();
      }
    },
  );

  it("stops every project's pages when the page goes away, quietly, and reads them again on its return", async () => {
    const { sent, spy, paths } = holding();
    const failed = vi.spyOn(console, "error");
    try {
      const all = queryClient.query(projectsQuery);
      await vi.waitFor(() => {
        expect(sent).toHaveLength(1);
      });
      sent[0]?.answer({ items: [{ name: "Kinase Atlas" }], next_cursor: "two" });
      await vi.waitFor(() => {
        expect(sent).toHaveLength(2);
      });
      const second = sent[1];
      transition("pagehide");
      try {
        expect(second?.request.signal.aborted).toBe(true);
        // Neither a failure to show or retry, nor a page after the one it stopped.
        await new Promise((resolve) => setTimeout(resolve, 50));
        expect(sent).toHaveLength(2);
        const state = queryClient.getQueryState(projectsQuery.queryKey);
        expect(state?.status).toBe("pending");
        expect(state?.error).toBeNull();
        expect(state?.fetchFailureCount).toBe(0);
      } finally {
        transition("pageshow");
      }
      await vi.waitFor(() => {
        expect(sent).toHaveLength(3);
      });
      sent[2]?.answer({ items: [{ name: "Kinase Atlas" }], next_cursor: "two" });
      await vi.waitFor(() => {
        expect(sent).toHaveLength(4);
      });
      sent[3]?.answer({ items: [{ name: "Assay Requests" }], next_cursor: null });
      await expect(all).resolves.toEqual([{ name: "Kinase Atlas" }, { name: "Assay Requests" }]);
      expect(paths()).toEqual([
        "?limit=100",
        "?limit=100&cursor=two",
        "?limit=100",
        "?limit=100&cursor=two",
      ]);
      expect(failed).not.toHaveBeenCalled();
    } finally {
      spy.mockRestore();
      failed.mockRestore();
    }
  });

  it("stops every project's pages when the query is cancelled in the app", async () => {
    const { sent, spy } = holding();
    try {
      const observer = new QueryObserver(queryClient, projectsQuery);
      const unsubscribe = observer.subscribe(() => undefined);
      await vi.waitFor(() => {
        expect(sent).toHaveLength(1);
      });
      sent[0]?.answer({ items: [{ name: "Kinase Atlas" }], next_cursor: "two" });
      await vi.waitFor(() => {
        expect(sent).toHaveLength(2);
      });
      // The form that wanted them closed.
      unsubscribe();
      expect(sent[1]?.request.signal.aborted).toBe(true);
      await new Promise((resolve) => setTimeout(resolve, 50));
      expect(sent).toHaveLength(2);
      const state = queryClient.getQueryState(projectsQuery.queryKey);
      expect(state?.error).toBeNull();
      expect(state?.fetchFailureCount).toBe(0);
    } finally {
      spy.mockRestore();
    }
  });

  it("still gives a loader the projects it awaits when the form that shared their read closes", async () => {
    const { sent, spy } = holding();
    try {
      const observer = new QueryObserver(queryClient, projectsQuery);
      const unsubscribe = observer.subscribe(() => undefined);
      await vi.waitFor(() => {
        expect(sent).toHaveLength(1);
      });
      // A route's loader asks for them while the form's read is on the wire, then the form goes.
      const loading = queryClient.query(projectsQuery);
      unsubscribe();
      expect(sent[0]?.request.signal.aborted).toBe(true);
      await vi.waitFor(() => {
        expect(sent).toHaveLength(2);
      });
      sent[1]?.answer({ items: [{ name: "Kinase Atlas" }], next_cursor: null });
      await expect(loading).resolves.toEqual([{ name: "Kinase Atlas" }]);
    } finally {
      spy.mockRestore();
    }
  });
});
