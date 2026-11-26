/** What a preview says under what it shows: what it read of the file, and what it left out. */

import type { ReactNode } from "react";

import { formatSize } from "../entities";
import type { ByteSource } from "./bytes";

export function PreviewNote({ children }: { children: ReactNode }) {
  return <p className="preview-note muted">{children}</p>;
}

export function Verified() {
  return <>Read whole and checked against its SHA-256.</>;
}

/** Whether the bytes shown were checked, or read in parts. */
export function Integrity({ source }: { source: ByteSource }) {
  if (source.verified) return <Verified />;
  return (
    <>
      Read in parts: {formatSize(source.bytesRead)} of {formatSize(source.size)} so far. Each part
      is checked to be the bytes asked for, but a file’s SHA-256 is checked only when all of it is
      read, as a download does.
    </>
  );
}

const number = new Intl.NumberFormat();

/** "12,345" */
export function count(n: number): string {
  return number.format(n);
}

/** "1 row", "2 rows". */
export function plural(n: number, one: string, many: string): string {
  return `${count(n)} ${n === 1 ? one : many}`;
}
