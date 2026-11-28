/**
 * A plugin's component's frame, from the frame's side, for tests: the port the page hands
 * it, what the page sends it, and a way to answer (ADR 0084, ADR 0117).
 */

import { PROTOCOL } from "@ikisci/component";
import { fireEvent } from "@testing-library/react";
import { vi } from "vitest";

import type { ComponentEntity } from "./components";

/** Loads the frame, takes the port the page hands it, and says it's ready. */
export function connectFrame(frame: HTMLIFrameElement) {
  const view = frame.contentWindow;
  if (!view) throw new Error("The frame has no window.");
  const post = vi.spyOn(view, "postMessage");
  fireEvent.load(frame);
  const [, , transfer] = post.mock.calls[0] as unknown as [unknown, string, MessagePort[]];
  const [port] = transfer as [MessagePort];
  const received: Record<string, unknown>[] = [];
  port.onmessage = (event: MessageEvent) => received.push(event.data as Record<string, unknown>);
  port.postMessage({ type: "ready", protocol: PROTOCOL });
  return { port, received };
}

/** The last `entities` the frame was sent. */
export function lastEntities(received: Record<string, unknown>[]) {
  const sent = received.filter((m) => m["type"] === "entities").at(-1);
  return sent?.["entities"] as
    { items: ComponentEntity[]; more: boolean; limited: boolean; refinement: unknown } | undefined;
}
