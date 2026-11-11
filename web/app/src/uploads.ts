/**
 * Uploading a file from the browser, as the files API takes it (ADR 0036):
 *
 * 1. Its SHA-256, read a slice at a time so a file of any size fits in
 *    memory. The page yields between slices, so it stays responsive.
 *    (A worker would be faster, but the CSP's Trusted Types allow no script
 *    URLs, workers' included.)
 * 2. `POST /uploads` with its name, type, size and hash: the answer lists its
 *    parts, each with a signed URL to object storage.
 * 3. Each part PUT straight to storage, a few at a time, with progress. A
 *    part that fails is sent again; one whose URL expired is signed again.
 * 4. `POST /uploads/{id}/complete`, then Iki reads the bytes back to check
 *    them against the hash, and the upload becomes a file.
 *
 * Cancelling stops the transfers and cancels the upload, so storage keeps
 * nothing.
 */

import { ApiError, unwrap, type Schemas } from "@ikisci/sdk";
import { sha256 } from "@noble/hashes/sha2.js";
import { bytesToHex } from "@noble/hashes/utils.js";

import { iki } from "./api";
import { media } from "./media";

export type UploadedFile = Schemas["FileOut"];

export type Progress =
  | { phase: "hashing"; done: number; total: number }
  | { phase: "sending"; done: number; total: number }
  | { phase: "checking" };

const SLICE = 1024 * 1024;
/** Parts in flight at once: enough to fill a lab's uplink, few enough for a laptop's. */
const PARALLEL = 4;
const TRIES = 4;

/** Waits, in milliseconds: before sending a part again, and between checks. Tests shorten them. */
export const timing = { retry: 500, poll: 500, maxPoll: 5000 };

export class UploadFailed extends Error {}

function aborted(signal: AbortSignal | undefined): void {
  if (signal?.aborted) throw new DOMException("The upload was cancelled.", "AbortError");
}

/** Lets the page draw and respond between slices. */
function yieldToPage(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

export async function sha256Of(
  file: Blob,
  onProgress?: (done: number) => void,
  signal?: AbortSignal,
): Promise<string> {
  const hash = sha256.create();
  for (let start = 0; start < file.size; start += SLICE) {
    aborted(signal);
    const slice = file.slice(start, Math.min(start + SLICE, file.size));
    hash.update(new Uint8Array(await slice.arrayBuffer()));
    onProgress?.(Math.min(start + SLICE, file.size));
    await yieldToPage();
  }
  return bytesToHex(hash.digest());
}

/** PUTs one part, reporting bytes sent. Resolves with the HTTP status. */
function put(
  url: string,
  body: Blob,
  onSent: (bytes: number) => void,
  signal: AbortSignal | undefined,
): Promise<number> {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open("PUT", url);
    request.upload.onprogress = (event) => {
      onSent(event.loaded);
    };
    request.onload = () => {
      resolve(request.status);
    };
    request.onerror = () => {
      resolve(0);
    };
    const abort = () => {
      request.abort();
      reject(new DOMException("The upload was cancelled.", "AbortError"));
    };
    signal?.addEventListener("abort", abort, { once: true });
    request.onloadend = () => {
      signal?.removeEventListener("abort", abort);
    };
    request.send(body);
  });
}

function wait(ms: number, signal: AbortSignal | undefined): Promise<void> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(resolve, ms);
    signal?.addEventListener(
      "abort",
      () => {
        clearTimeout(timer);
        reject(new DOMException("The upload was cancelled.", "AbortError"));
      },
      { once: true },
    );
  });
}

export async function upload(
  file: File,
  projectId: string,
  onProgress: (progress: Progress) => void,
  signal?: AbortSignal,
): Promise<UploadedFile> {
  onProgress({ phase: "hashing", done: 0, total: file.size });
  const hash = await sha256Of(
    file,
    (done) => {
      onProgress({ phase: "hashing", done, total: file.size });
    },
    signal,
  );
  aborted(signal);
  const started = unwrap(
    await iki.POST("/api/v1/uploads", {
      body: {
        project: projectId,
        name: file.name,
        media_type: media.of(file),
        size: file.size,
        sha256: hash,
      },
    }),
  );
  try {
    if (started.state === "ready" && started.file) return started.file;
    await sendParts(file, started, onProgress, signal);
    aborted(signal);
    unwrap(
      await iki.POST("/api/v1/uploads/{upload_id}/complete", {
        params: { path: { upload_id: started.id } },
      }),
    );
    onProgress({ phase: "checking" });
    return await finished(started.id, signal);
  } catch (error) {
    if (signal?.aborted) {
      // Nothing half-sent is kept.
      void iki.DELETE("/api/v1/uploads/{upload_id}", {
        params: { path: { upload_id: started.id } },
      });
    }
    throw error;
  }
}

async function sendParts(
  file: File,
  started: Schemas["StartedOut"],
  onProgress: (progress: Progress) => void,
  signal: AbortSignal | undefined,
): Promise<void> {
  const sent = new Map<number, number>();
  const report = () => {
    let done = 0;
    for (const bytes of sent.values()) done += bytes;
    onProgress({ phase: "sending", done, total: file.size });
  };
  report();
  const queue = [...started.parts];
  const sendOne = async (part: Schemas["PartOut"]) => {
    const start = (part.number - 1) * started.part_size;
    const body = file.slice(start, start + part.size);
    let url = part.url;
    for (let attempt = 1; ; attempt++) {
      aborted(signal);
      const status = await put(
        url,
        body,
        (bytes) => {
          sent.set(part.number, bytes);
          report();
        },
        signal,
      );
      if (status >= 200 && status < 300) {
        sent.set(part.number, part.size);
        report();
        return;
      }
      sent.set(part.number, 0);
      if (attempt >= TRIES) {
        throw new UploadFailed(
          status === 0
            ? "The file couldn’t reach storage. Check your connection, then try again."
            : `Storage refused part ${String(part.number)} (HTTP ${String(status)}). Try again.`,
        );
      }
      if (status === 403) {
        // Its URL expired (they last an hour): sign it again.
        const fresh = unwrap(
          await iki.POST("/api/v1/uploads/{upload_id}/parts", {
            params: { path: { upload_id: started.id } },
            body: { numbers: [part.number] },
          }),
        );
        url = fresh[0]?.url ?? url;
      }
      await wait(timing.retry * 2 ** (attempt - 1), signal);
    }
  };
  const lanes = Array.from({ length: Math.min(PARALLEL, queue.length) }, async () => {
    for (let part = queue.shift(); part; part = queue.shift()) await sendOne(part);
  });
  await Promise.all(lanes);
}

/** Waits for Iki to check the bytes. Large files take a while: it reads them back. */
async function finished(uploadId: string, signal: AbortSignal | undefined): Promise<UploadedFile> {
  for (let delay = timing.poll; ; delay = Math.min(delay * 1.5, timing.maxPoll)) {
    const current = unwrap(
      await iki.GET("/api/v1/uploads/{upload_id}", { params: { path: { upload_id: uploadId } } }),
    );
    if (current.state === "ready" && current.file) return current.file;
    if (current.state !== "checking" && current.state !== "pending") {
      throw new UploadFailed(current.error || `The upload ${current.state}.`);
    }
    await wait(delay, signal);
  }
}

/** What went wrong, in words, for any error an upload throws. */
export function uploadError(error: unknown): string {
  if (error instanceof UploadFailed) return error.message;
  if (error instanceof ApiError) return error.problem.errors?.[0]?.detail ?? error.problem.detail;
  return "The upload failed. Check your connection, then try again.";
}
