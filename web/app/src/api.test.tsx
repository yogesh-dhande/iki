/**
 * Mutations call back once they end, even when the component that started one
 * has gone: what they refresh often replaces it.
 */

import { ApiError } from "@ikisci/sdk";
import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import { queryClient, useApiMutation } from "./api";

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

function later<T>(): { promise: Promise<T>; resolve: (v: T) => void; reject: (e: unknown) => void } {
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
