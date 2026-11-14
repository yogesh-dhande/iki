// Iki's bridge, in a component's sandboxed frame (ADR 0084). It creates the component's
// element, gives it what the page sends, and passes on what the element tells the page,
// through the one port the page hands it. The page checks every message itself: the
// bridge shares the frame with the component's code, so it's a convenience, not a guard.
const config = JSON.parse(document.getElementById("iki-component").textContent);
let port = null;
let element = null;

function send(message) {
  port.postMessage(message);
}

function selected(event) {
  const picked = event.detail?.entities;
  if (!Array.isArray(picked)) return;
  const entities = picked.map((e) => (typeof e === "string" ? e : (e?.hrid ?? e?.id)));
  send({ type: "select", entities: entities.filter((e) => typeof e === "string") });
}

function received(event) {
  const message = event.data;
  if (message?.type === "entity" && element) element.entity = message.entity;
}

async function start() {
  await customElements.whenDefined(config.element);
  element = document.createElement(config.element);
  element.addEventListener("iki-select", selected);
  document.body.append(element);
  const size = () => send({ type: "resize", height: document.documentElement.scrollHeight });
  new ResizeObserver(size).observe(document.body);
  send({ type: "ready" });
}

function connected(event) {
  if (event.source !== window.parent || event.data?.type !== "iki-connect") return;
  if (event.ports.length !== 1) return;
  window.removeEventListener("message", connected);
  port = event.ports[0];
  port.onmessage = received;
  void start();
}

window.addEventListener("message", connected);
