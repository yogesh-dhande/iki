/**
 * Iki's bridge in a component's frame speaks @ikisci/component's protocol (ADR 0286): it
 * connects only for the package's PROTOCOL, forwards exactly the package's EVENTS, and
 * what it forwards from each of the package's helpers is what the page's validators read.
 * The bridge runs here as the server serves it, in a frame's document of its own.
 */

import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import * as component from "@ikisci/component";
import { readMessage, type Declaration } from "@ikisci/component/host";
import { afterEach, describe, expect, it, vi } from "vitest";

const BRIDGE = readFileSync(
  resolve(__dirname, "../../../server/src/iki_server/components/bridge.js"),
  "utf8",
);
const ELEMENT = "bridge-test-element";
customElements.define(ELEMENT, class extends HTMLElement {});
// eslint-disable-next-line @typescript-eslint/unbound-method -- applied to its target below
const listen = EventTarget.prototype.addEventListener;

/** What the component declared: it picks, shows many and offers one action. */
const DECLARED: Declaration = {
  selects: "acme/protein.Protein",
  entities: { fields: ["name"] },
  runs: ["acme/protein.compute"],
};

interface Frame {
  sent: unknown[];
  listened: string[];
  element: () => HTMLElement;
}

/** Runs the bridge in this document, as the frame does, and connects to it. */
async function bridge(protocol: unknown = component.PROTOCOL): Promise<Frame> {
  vi.restoreAllMocks();
  document.body.innerHTML = "";
  const config = document.createElement("script");
  config.type = "application/json";
  config.id = "iki-component";
  config.textContent = JSON.stringify({ element: ELEMENT });
  document.head.replaceChildren(config);
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {
        // The frame's height isn't what this test is about.
      }
    },
  );
  const sent: unknown[] = [];
  const port = { postMessage: (message: unknown) => sent.push(message), onmessage: null };
  // The frame's window, as the bridge sees it: the page is its parent.
  const parent = {};
  const heard: ((event: unknown) => void)[] = [];
  const frame = {
    parent,
    addEventListener: (_: string, listener: (event: unknown) => void) => heard.push(listener),
    removeEventListener: (_: string, listener: (event: unknown) => void) =>
      heard.splice(heard.indexOf(listener), 1),
  };
  // What it listens for on its element.
  const listened: string[] = [];
  vi.spyOn(HTMLElement.prototype, "addEventListener").mockImplementation(function (
    this: HTMLElement,
    ...args: Parameters<typeof listen>
  ) {
    if (this.localName === ELEMENT) listened.push(args[0]);
    listen.apply(this, args);
  });
  // The bridge as the server serves it, in a module of its own.
  // eslint-disable-next-line @typescript-eslint/no-implied-eval -- the bridge as served
  const run = new Function("window", BRIDGE) as (window: unknown) => void;
  run(frame);
  const connect = { source: parent, data: { type: "iki-connect", protocol }, ports: [port] };
  for (const listener of [...heard]) listener(connect);
  await new Promise((resolve) => setTimeout(resolve, 0));
  return {
    sent,
    listened,
    element: () => {
      const found = document.querySelector<HTMLElement>(ELEMENT);
      if (!found) throw new Error("The bridge made no element.");
      return found;
    },
  };
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("Iki's bridge", () => {
  it("connects for the package's protocol, and says so when it's ready", async () => {
    const frame = await bridge();
    expect(frame.sent).toEqual([{ type: "ready", protocol: component.PROTOCOL }]);
    expect(readMessage(DECLARED, frame.sent[0])).toEqual({
      type: "ready",
      protocol: component.PROTOCOL,
    });
  });

  it("doesn't answer a page that speaks another version", async () => {
    for (const protocol of [component.PROTOCOL + 1, null, "1"]) {
      const frame = await bridge(protocol);
      expect(frame.sent).toEqual([]);
    }
  });

  it("forwards exactly the package's events", async () => {
    const frame = await bridge();
    expect(new Set(frame.listened)).toEqual(new Set(Object.values(component.EVENTS)));
    expect(frame.listened).toHaveLength(Object.values(component.EVENTS).length);
    // Anything else an element sends never reaches the page.
    frame.sent.length = 0;
    frame
      .element()
      .dispatchEvent(new CustomEvent("iki-navigate", { detail: { to: "/" }, bubbles: true }));
    expect(frame.sent).toEqual([]);
  });

  it("passes on what each helper asks for as the page reads it", async () => {
    const frame = await bridge();
    const element = frame.element();
    const record = "00000000-0000-7000-8000-00000000e002";
    const narrower = { where: { field: "name", op: "starts_with" as const, value: "AB" } };
    const asked: [() => void, unknown][] = [
      [
        () => {
          component.select(element, ["PRT-000001"]);
        },
        { type: "select", entities: ["PRT-000001"] },
      ],
      [
        () => {
          component.more(element);
        },
        { type: "more" },
      ],
      [
        () => {
          component.refine(element, narrower);
        },
        { type: "refine", refinement: narrower },
      ],
      [
        () => {
          component.refine(element, null);
        },
        { type: "refine", refinement: null },
      ],
      [
        () => {
          component.open(element, "PRT-000002");
        },
        { type: "open", entity: "PRT-000002" },
      ],
      [
        () => {
          component.open(element, record, 4);
        },
        { type: "open", entity: record, revision: 4 },
      ],
      [
        () => {
          component.run(element, "acme/protein.compute");
        },
        { type: "run", action: "acme/protein.compute" },
      ],
    ];
    // Each of the package's events is one of these.
    const helpers = ["select", "more", "refine", "open", "run"] as const;
    expect(new Set(helpers.map((h) => component.EVENTS[h]))).toEqual(
      new Set(Object.values(component.EVENTS)),
    );
    for (const [ask, message] of asked) {
      frame.sent.length = 0;
      ask();
      expect(frame.sent).toEqual([message]);
      expect(readMessage(DECLARED, frame.sent[0])).toEqual(message);
    }
  });
});
