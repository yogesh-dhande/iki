/**
 * Reading a stored file's bytes in the page, for viewers that draw files themselves: from
 * the store, with a link Iki signs, checked against the SHA-256 Iki recorded when it was
 * stored, so a viewer never shows bytes that aren't the file.
 */

import { unwrap } from "@ikisci/sdk";

import { iki } from "./api";

async function hex(bytes: ArrayBuffer): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
}

/** The file's bytes, read from the store and checked against the hash Iki recorded. */
export async function readStoredFile(fileId: string, signal: AbortSignal): Promise<ArrayBuffer> {
  const link = unwrap(
    await iki.GET("/api/v1/files/{file_id}/content-url", {
      params: { path: { file_id: fileId } },
      signal,
    }),
  );
  const response = await fetch(link.url, { signal, credentials: "omit" });
  if (!response.ok) throw new Error(`The file store answered ${String(response.status)}.`);
  const bytes = await response.arrayBuffer();
  if ((await hex(bytes)) !== link.sha256) {
    throw new Error("What the file store sent isn’t the file Iki stored, so Iki didn’t show it.");
  }
  return bytes;
}
