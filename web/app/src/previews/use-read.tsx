/** Reading for a viewer: what it's reading, what it read, or why it couldn't. */

import { useEffect, useState, type DependencyList } from "react";

import { NoPreview } from "./no-preview";
import { problemCode, readMessage, type PreviewFile } from "./source";

export type Read<T> =
  | { phase: "reading" }
  | { phase: "read"; value: T }
  | { phase: "failed"; message: string; held: boolean };

/** Problems the server gives for a file its scan holds (62g): the download is refused too. */
const HELD = new Set(["file_scanning", "file_quarantined", "file_not_scanned"]);

function same(a: DependencyList, b: DependencyList): boolean {
  return a.length === b.length && a.every((value, i) => Object.is(value, b[i]));
}

const READING = { phase: "reading" } as const;

/** Runs `read` when the deps change, cancelling the last one. */
export function useRead<T>(
  read: (signal: AbortSignal) => Promise<T>,
  deps: DependencyList,
): Read<T> {
  // What was read, and for which deps: a change of deps reads as "reading" until it's done.
  const [done, setDone] = useState<{ deps: DependencyList; read: Read<T> } | null>(null);
  useEffect(() => {
    const abort = new AbortController();
    read(abort.signal).then(
      (value) => {
        if (!abort.signal.aborted) setDone({ deps, read: { phase: "read", value } });
      },
      (error: unknown) => {
        if (abort.signal.aborted) return;
        const code = problemCode(error);
        setDone({
          deps,
          read: {
            phase: "failed",
            message: readMessage(error),
            held: code !== undefined && HELD.has(code),
          },
        });
      },
    );
    return () => {
      abort.abort();
    };
    // The caller's deps are what the read depends on.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  return done && same(done.deps, deps) ? done.read : READING;
}

/** Why it couldn't be read, with the download unless the download is refused too. */
export function ReadFailed({
  file,
  failed,
}: {
  file: PreviewFile;
  failed: Extract<Read<unknown>, { phase: "failed" }>;
}) {
  return (
    <NoPreview file={file} download={!failed.held}>
      {failed.held ? failed.message : `Iki couldn’t show ${file.name}. ${failed.message}`}
    </NoPreview>
  );
}
