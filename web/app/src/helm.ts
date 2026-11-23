/**
 * HELM (ADR 0144), read as it's typed by iki-wasm in the expressions worker: the module
 * the server reads it with, so what a form says of a HELM string is what a save will.
 */

import type { HelmReading } from "@ikisci/science/parsers";
import { useEffect, useState } from "react";

import { expressionRenderer } from "./apps";
import { SETTLE_MS } from "./rules";

export { describeHelm, isProblem } from "@ikisci/science/parsers";

/** Each HELM string read, or why it isn't HELM. */
export async function readHelm(texts: string[]): Promise<HelmReading[]> {
  if (texts.length === 0) return [];
  return (await expressionRenderer().helm(JSON.stringify({ helm: texts }))) as HelmReading[];
}

/** `text` read once it has settled, as it's typed; null while it's empty or waiting. */
export function useHelmReading(text: string): HelmReading | null {
  const [reading, setReading] = useState<{ text: string; reading: HelmReading } | null>(null);
  useEffect(() => {
    if (!text.trim()) return undefined;
    let current = true;
    const timer = setTimeout(() => {
      readHelm([text]).then(
        ([found]) => {
          if (current && found) setReading({ text, reading: found });
        },
        () => {
          // The worker didn't load: the server reads it at the save.
        },
      );
    }, SETTLE_MS);
    return () => {
      current = false;
      clearTimeout(timer);
    };
  }, [text]);
  return text.trim() && reading?.text === text ? reading.reading : null;
}
