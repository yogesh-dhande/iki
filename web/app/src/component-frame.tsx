/**
 * A plugin's component in a sandboxed frame of its own (ADR 0084). The frame's document
 * has an origin that's no one's, so it can't read Iki's cookies, storage or pages, or
 * call the API as the person. The page hands Iki's bridge in it one port, gives it the
 * entity, and checks every message it sends back.
 */

import { useEffect, useRef, useState, type SyntheticEvent } from "react";

import {
  MIN_FRAME_HEIGHT,
  readMessage,
  type FrameMessage,
  type PluginComponent,
} from "./components";
import type { Entity } from "./entities";
import { Loading } from "./ui";

/** How long a component has to start before the page says it didn't. */
export const START_TIMEOUT_MS = 15_000;

type State = "starting" | "ready" | "failed" | "left";

export function ComponentFrame({
  component,
  label,
  entity,
  onSelect,
}: {
  component: PluginComponent;
  label: string;
  entity: Entity;
  onSelect?: (entities: string[]) => void;
}) {
  const port = useRef<MessagePort | null>(null);
  const loads = useRef(0);
  const selected = useRef(onSelect);
  const [state, setState] = useState<State>("starting");
  const [height, setHeight] = useState(MIN_FRAME_HEIGHT);

  useEffect(() => {
    selected.current = onSelect;
  }, [onSelect]);
  useEffect(() => () => port.current?.close(), []);
  useEffect(() => {
    if (state === "ready") port.current?.postMessage({ type: "entity", entity });
  }, [entity, state]);
  useEffect(() => {
    if (state !== "starting") return;
    const timer = setTimeout(() => {
      setState((now) => (now === "starting" ? "failed" : now));
    }, START_TIMEOUT_MS);
    return () => {
      clearTimeout(timer);
    };
  }, [state]);

  function received(message: FrameMessage) {
    if (message.type === "ready") setState((now) => (now === "left" ? now : "ready"));
    else if (message.type === "resize") setHeight(message.height);
    else selected.current?.(message.entities);
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
    event.currentTarget.contentWindow?.postMessage({ type: "iki-connect" }, "*", [channel.port2]);
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
