/**
 * A previewed file's bytes, read from the store (ADR 0226, ADR 0227). Every read starts
 * with `/files/{id}/content-url`: the same link, access check and audit (`file.download_link_
 * issued`) as a download, so a preview reaches no file its reader couldn't download, and is
 * recorded as a download is. A file read whole is checked against the SHA-256 Iki recorded
 * when it stored it. A large file is read in ranges instead (a TIFF's tiles, an mzML file's
 * spectra): each range is checked to be the bytes asked for, of a file the size Iki
 * recorded, but the whole file's SHA-256 can't be, without reading all of it.
 */

import { ApiError, unwrap, type Schemas } from "@ikisci/sdk";

import { iki } from "../api";

type ContentUrl = Schemas["ContentUrlOut"];

/** What a preview knows of its file. */
export interface PreviewFile {
  id: string;
  name: string;
  media_type: string;
  size: number;
  sha256: string;
}

/** A link is asked for again this long before it expires, so a read never starts on one
 * that ends part way. */
const RENEW_MS = 30_000;

/** Why bytes couldn't be read, for a person. */
export class ReadError extends Error {}

/** The most one preview reads in all, ranges included: a preview is a look, not a copy. */
export const MAX_PREVIEW_BYTES = 512 * 1024 * 1024;

async function sha256(bytes: ArrayBuffer): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
}

/** `Content-Range: bytes 0-99/1234` → its first and last byte and the total, or null. */
export function parseContentRange(
  value: string | null,
): { first: number; last: number; total: number } | null {
  const match = value ? /^bytes (\d+)-(\d+)\/(\d+)$/.exec(value.trim()) : null;
  if (!match) return null;
  const [first, last, total] = [match[1], match[2], match[3]].map(Number) as [
    number,
    number,
    number,
  ];
  return Number.isSafeInteger(total) && first <= last && last < total
    ? { first, last, total }
    : null;
}

/** One file's bytes, as one preview reads them. */
export class StoredBytes {
  private link: ContentUrl | null = null;
  private asking: Promise<ContentUrl> | null = null;
  /** How many bytes this preview has read. */
  read = 0;

  constructor(
    readonly file: PreviewFile,
    private readonly budget: number = MAX_PREVIEW_BYTES,
  ) {}

  /** A signed link to the file, asked for again when it's about to expire. */
  private async url(signal: AbortSignal): Promise<string> {
    const fresh = this.link && Date.parse(this.link.expires_at) - Date.now() > RENEW_MS;
    if (fresh && this.link) return this.link.url;
    this.asking ??= iki
      .GET("/api/v1/files/{file_id}/content-url", {
        params: { path: { file_id: this.file.id } },
        signal,
      })
      .then(unwrap)
      .finally(() => {
        this.asking = null;
      });
    this.link = await this.asking;
    if (this.link.sha256 !== this.file.sha256) {
      throw new ReadError("The link Iki gave is for other bytes than this file’s.");
    }
    return this.link.url;
  }

  private spend(bytes: number): void {
    if (this.read + bytes > this.budget) {
      throw new ReadError(
        "This preview has read as much of the file as a preview may. Download it to see the rest.",
      );
    }
    this.read += bytes;
  }

  /** The whole file, checked against its SHA-256. */
  async whole(signal: AbortSignal): Promise<ArrayBuffer> {
    this.spend(this.file.size);
    const response = await fetch(await this.url(signal), { signal, credentials: "omit" });
    if (!response.ok) throw new ReadError(`The file store answered ${String(response.status)}.`);
    const bytes = await response.arrayBuffer();
    if (bytes.byteLength !== this.file.size || (await sha256(bytes)) !== this.file.sha256) {
      throw new ReadError(
        "What the file store sent isn’t the file Iki stored, so Iki didn’t show it.",
      );
    }
    return bytes;
  }

  /** `length` bytes from `offset` (fewer at the file's end), as the store sent them. */
  async range(offset: number, length: number, signal: AbortSignal): Promise<Uint8Array> {
    const end = Math.min(offset + length, this.file.size);
    if (offset < 0 || offset >= end) return new Uint8Array(0);
    this.spend(end - offset);
    const response = await fetch(await this.url(signal), {
      signal,
      credentials: "omit",
      headers: { Range: `bytes=${String(offset)}-${String(end - 1)}` },
    });
    // A store that ignores Range would send the whole file: stop it rather than read it.
    if (response.status !== 206) {
      await response.body?.cancel();
      throw new ReadError(
        response.ok
          ? "The file store sent the whole file when asked for part of it, so Iki stopped reading it."
          : `The file store answered ${String(response.status)}.`,
      );
    }
    const header = response.headers.get("Content-Range");
    if (header === null) {
      await response.body?.cancel();
      throw new ReadError(
        "The file store doesn’t let pages see which bytes it sent. An administrator can allow it by running iki-server setup_storage.",
      );
    }
    const range = parseContentRange(header);
    if (range?.first !== offset || range.last !== end - 1) {
      await response.body?.cancel();
      throw new ReadError("The file store sent other bytes than those asked for.");
    }
    if (range.total !== this.file.size) {
      await response.body?.cancel();
      throw new ReadError(
        "The file store’s copy isn’t the size Iki recorded, so Iki didn’t show it.",
      );
    }
    const bytes = new Uint8Array(await response.arrayBuffer());
    if (bytes.byteLength !== end - offset) {
      throw new ReadError("The file store sent fewer bytes than it said.");
    }
    return bytes;
  }
}

/** A problem code from the server, if `error` is one: a held file's, say (62g). */
export function problemCode(error: unknown): string | undefined {
  return error instanceof ApiError ? error.code : undefined;
}

/** What to say about a read that failed. */
export function readMessage(error: unknown): string {
  if (error instanceof ApiError) return error.problem.detail;
  if (error instanceof DOMException && error.name === "AbortError") return "";
  if (error instanceof Error) return error.message;
  return String(error);
}
