/**
 * A plugin's component in a sandboxed frame of its own (ADR 0084). The frame's document
 * has an origin that's no one's, so it can't read Iki's cookies, storage or pages, or
 * call the API as the person. The page hands Iki's bridge in it one port, gives it the
 * entity and the many entities its place shows (ADR 0117), and checks every message it
 * sends back: more, a refinement and an entity to open only for what it was given, and an
 * action to run only one its place offers (ADR 0170).
 */

import { useEffect, useRef, useState, type SyntheticEvent } from "react";

import {
  MIN_FRAME_HEIGHT,
  PROTOCOL,
  type Connect,
  type Entity,
  type HostMessage,
  type Place,
} from "@ikisci/component";
import { readMessage, type ReadMessage, type ReadRefinement } from "@ikisci/component/host";

import type { PlaceEntities } from "./component-entities";
import { pinnedRevisionUrl, type PluginComponent } from "./components";
import { Loading } from "./ui";

/** How long a component has to start before the page says it didn't. */
export const START_TIMEOUT_MS = 15_000;

type State = "starting" | "ready" | "failed" | "left";

/** What the frame is given: the entity whose page it is, or that a panel names. */
export type GivenEntity = Entity;

export interface FrameProps {
  component: PluginComponent;
  label: string;
  place: Place;
  entity?: GivenEntity | undefined;
  entities?: PlaceEntities | undefined;
  onSelect?: ((entities: string[]) => void) | undefined;
  onMore?: (() => void) | undefined;
  onRefine?: ((refinement: ReadRefinement | null) => void) | undefined;
  onOpen?: ((hrid: string) => void) | undefined;
  /** A person asked to run one of the actions its place offers, on its entity. */
  onRun?: ((action: string) => void) | undefined;
  /** It can't be shown: it didn't start, or opened another page. */
  onFailed?: (() => void) | undefined;
}

export function ComponentFrame(props: FrameProps) {
  const { component, label, place, entity, entities } = props;
  const port = useRef<MessagePort | null>(null);
  const loads = useRef(0);
  const latest = useRef(props);
  const [state, setState] = useState<State>("starting");
  const [height, setHeight] = useState(MIN_FRAME_HEIGHT);

  useEffect(() => {
    latest.current = props;
  });
  useEffect(() => () => port.current?.close(), []);
  /** What the page tells the bridge, which sets it on the element. */
  function post(message: HostMessage) {
    port.current?.postMessage(message);
  }
  const placeKey = JSON.stringify(place);
  useEffect(() => {
    if (state === "ready") post({ type: "place", place });
    // `placeKey` stands for the place.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [placeKey, state]);
  useEffect(() => {
    if (state === "ready" && entity) post({ type: "entity", entity });
  }, [entity, state]);
  useEffect(() => {
    if (state === "ready" && entities) post({ type: "entities", entities });
  }, [entities, state]);
  useEffect(() => {
    if (state !== "starting") return;
    const timer = setTimeout(() => {
      setState((now) => (now === "starting" ? "failed" : now));
    }, START_TIMEOUT_MS);
    return () => {
      clearTimeout(timer);
    };
  }, [state]);
  useEffect(() => {
    if (state === "failed" || state === "left") latest.current.onFailed?.();
  }, [state]);

  function received(message: ReadMessage) {
    const now = latest.current;
    switch (message.type) {
      case "ready":
        setState((current) => (current === "left" ? current : "ready"));
        return;
      case "resize":
        setHeight(message.height);
        return;
      case "select":
        now.onSelect?.(message.entities);
        return;
      case "more":
        if (now.entities) now.onMore?.();
        return;
      case "refine":
        if (now.entities) now.onRefine?.(message.refinement);
        return;
      case "run":
        if (now.place.runs?.includes(message.action)) now.onRun?.(message.action);
        return;
      case "open": {
        if (message.revision !== undefined) {
          const url = pinnedRevisionUrl(now.entity, message.entity, message.revision);
          if (url) window.location.assign(url);
          return;
        }
        // Only an entity it was given: Iki opens its page, as the person may read it.
        const given = new Set([
          ...(now.entity ? [now.entity.hrid.toUpperCase()] : []),
          ...(now.entities?.items.map((e) => e.hrid.toUpperCase()) ?? []),
        ]);
        if (given.has(message.entity)) now.onOpen?.(message.entity);
        return;
      }
    }
  }

  function loaded(event: SyntheticEvent<HTMLIFrameElement>) {
    loads.current += 1;
    if (loads.current > 1) {
      // It opened another page in its frame, which no policy can stop: whatever that is,
      // it isn't the component Iki checked. Stop talking to it, and take it away.
      port.current?.close();
      port.current = null;
      setState("left");
      return;
    }
    const channel = new MessageChannel();
    channel.port1.onmessage = (incoming) => {
      const message = readMessage(component, incoming.data);
      if (message) received(message);
    };
    port.current = channel.port1;
    // Its origin is no one's, so there's no origin to name but "*": the port is what's private.
    // The bridge connects only for the protocol it speaks, and says so when it's ready.
    const connect: Connect = { type: "iki-connect", protocol: PROTOCOL };
    event.currentTarget.contentWindow?.postMessage(connect, "*", [channel.port2]);
  }

  if (state === "left") {
    return (
      <div className="alert alert-error" role="alert">
        <p>
          {label} tried to open another page, so Iki closed it. Tell whoever looks after the{" "}
          {component.plugin} plugin.
        </p>
      </div>
    );
  }
  return (
    <>
      {state === "starting" ? <Loading label={`Starting ${label}…`} /> : null}
      {state === "failed" ? (
        <div className="alert alert-error" role="alert">
          <p>
            {label} didn’t start. Reload the page to try again, or tell whoever looks after the{" "}
            {component.plugin} plugin.
          </p>
        </div>
      ) : null}
      <iframe
        title={label}
        src={component.frame}
        sandbox="allow-scripts"
        referrerPolicy="no-referrer"
        className="component-frame"
        style={{ height: `${String(height)}px` }}
        onLoad={loaded}
      />
    </>
  );
}
