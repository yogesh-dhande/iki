/**
 * Where a viewer reads a file from: all of it, read once and checked against its SHA-256,
 * when it's small enough; otherwise ranges of it, read from the store as they're needed
 * (ADR 0227). Either way a viewer asks for `read(offset, length)`, and says which it was.
 */

import { StoredBytes } from "./source";

export interface ByteSource {
  readonly size: number;
  /** Whether the bytes were checked against the file's SHA-256: all of them were read. */
  readonly verified: boolean;
  read: (offset: number, length: number, signal: AbortSignal) => Promise<Uint8Array>;
  /** How many bytes have been read from the store. */
  readonly bytesRead: number;
}

/** A file read whole, checked, and served from memory. */
export class WholeFile implements ByteSource {
  readonly verified = true;
  readonly bytes: Uint8Array;
  constructor(buffer: ArrayBuffer) {
    this.bytes = new Uint8Array(buffer);
  }
  get size(): number {
    return this.bytes.byteLength;
  }
  get bytesRead(): number {
    return this.bytes.byteLength;
  }
  read(offset: number, length: number): Promise<Uint8Array> {
    const start = Math.max(0, Math.min(offset, this.size));
    return Promise.resolve(this.bytes.subarray(start, Math.min(this.size, start + length)));
  }
}

/** A file read in ranges, as they're needed. */
export class RangedFile implements ByteSource {
  readonly verified = false;
  constructor(private readonly stored: StoredBytes) {}
  get size(): number {
    return this.stored.file.size;
  }
  get bytesRead(): number {
    return this.stored.read;
  }
  read(offset: number, length: number, signal: AbortSignal): Promise<Uint8Array> {
    return this.stored.range(offset, length, signal);
  }
}

/** The file whole if it's at most `whole` bytes, else in ranges. */
export async function openBytes(
  stored: StoredBytes,
  whole: number,
  signal: AbortSignal,
): Promise<ByteSource> {
  if (stored.file.size <= whole) return new WholeFile(await stored.whole(signal));
  return new RangedFile(stored);
}
