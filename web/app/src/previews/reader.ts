/**
 * The page's side of the previews' worker (workers/previews.ts): one worker per preview,
 * ended with it, since WebAssembly's memory never shrinks. A read that runs past its time
 * ends the worker, and the next read starts another: a hostile file can make a reader slow,
 * but not the page.
 */

import { newWorker } from "../reading";
import previewsUrl from "../workers/previews.ts?worker&url";
import type {
  Chromatogram,
  MzmlElement,
  MzmlIndex,
  Spectra,
  Table,
  TiffStep,
  TiffTile,
} from "./readings";
import { ReadError } from "./source";
import type { Operation } from "./wasm";

/** What each reader answers. */
export interface Answers {
  abif: Chromatogram;
  jcamp: Spectra;
  mzml_offset: { index_offset: number | null };
  mzml_index: MzmlIndex;
  mzml_scan: MzmlIndex;
  mzml_element: MzmlElement;
  tiff_layout: TiffStep;
  tiff_tile: TiffTile;
  csv: Table;
  xlsx: Table;
}

/** How long one read may take. */
export const READ_TIMEOUT_MS = 30_000;

interface Pending {
  resolve: (answer: { json: string; pixels: ArrayBuffer | null }) => void;
  reject: (error: Error) => void;
  timer: ReturnType<typeof setTimeout>;
}

export class PreviewReader {
  private worker: Worker | null = null;
  private next = 1;
  private readonly pending = new Map<number, Pending>();

  constructor(private readonly start: () => Worker = () => newWorker(previewsUrl)) {}

  private open(): Worker {
    if (this.worker) return this.worker;
    const worker = this.start();
    worker.addEventListener(
      "message",
      (
        event: MessageEvent<{ id: number; json?: string; pixels?: ArrayBuffer; error?: string }>,
      ) => {
        const { id, json, pixels, error } = event.data;
        const waiting = this.pending.get(id);
        if (!waiting) return;
        this.pending.delete(id);
        clearTimeout(waiting.timer);
        if (json !== undefined) waiting.resolve({ json, pixels: pixels ?? null });
        else waiting.reject(new ReadError(error ?? "The readers stopped."));
      },
    );
    worker.addEventListener("error", () => {
      this.fail(new ReadError("The readers stopped."));
    });
    this.worker = worker;
    return worker;
  }

  private fail(error: Error): void {
    this.worker?.terminate();
    this.worker = null;
    for (const waiting of this.pending.values()) {
      clearTimeout(waiting.timer);
      waiting.reject(error);
    }
    this.pending.clear();
  }

  /** Runs one reader on `input` (which is copied), and parses its JSON. */
  async read<O extends Operation>(
    operation: O,
    args: readonly number[],
    input: Uint8Array,
    timeout = READ_TIMEOUT_MS,
  ): Promise<{ value: Answers[O]; pixels: ArrayBuffer | null }> {
    const worker = this.open();
    const id = this.next++;
    const copy = input.slice().buffer;
    const answer = await new Promise<{ json: string; pixels: ArrayBuffer | null }>(
      (resolve, reject) => {
        const timer = setTimeout(() => {
          this.fail(
            new ReadError(
              `Reading it took more than ${String(timeout / 1000)} seconds, so Iki stopped. Download it to open it in a desktop program.`,
            ),
          );
        }, timeout);
        this.pending.set(id, { resolve, reject, timer });
        worker.postMessage({ id, operation, args, input: copy }, [copy]);
      },
    );
    // The reader's own JSON: what its Rust types serialise (readings.ts), or why it failed.
    const value = JSON.parse(answer.json) as Answers[O] | { error: string };
    if ("error" in value && typeof value.error === "string") throw new ReadError(value.error);
    return { value: value as Answers[O], pixels: answer.pixels };
  }

  close(): void {
    this.fail(new ReadError("The preview closed."));
  }
}
