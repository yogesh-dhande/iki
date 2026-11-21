// Iki's bridge, in a component's sandboxed frame (ADR 0084, ADR 0117, ADR 0169). It creates
// the component's element, gives it what the page sends (the entity whose page it is, the
// many entities its place shows, and where it is, with the actions it may offer), and
// passes on what the element tells the page, through the one port the page hands it. The
// page checks every message itself: the bridge shares the frame with the component's
// code, so it's a convenience, not a guard.
const config = JSON.parse(document.getElementById("iki-component").textContent);
let port = null;
let element = null;

function send(message) {
  port.postMessage(message);
}

function hridOf(entity) {
  return typeof entity === "string" ? entity : entity?.hrid;
}

function selected(event) {
  const picked = event.detail?.entities;
  if (!Array.isArray(picked)) return;
  // HRIDs, or the entities Iki gave it.
  const entities = picked.map(hridOf);
  send({ type: "select", entities: entities.filter((e) => typeof e === "string") });
}

function refined(event) {
  // Only what can be copied: the page reads it again, and refuses anything else.
  let refinement;
  try {
    refinement = event.detail == null ? null : JSON.parse(JSON.stringify(event.detail));
  } catch {
    return;
  }
  send({ type: "refine", refinement });
}

function opened(event) {
  const entity = hridOf(event.detail?.entity);
  if (typeof entity === "string") send({ type: "open", entity });
}

function ran(event) {
  // An action its place offers, to run on its entity: the page opens Iki's run page.
  const action = event.detail?.action;
  if (typeof action === "string") send({ type: "run", action });
}

// What the page sets on the element, each by the message of its name.
const PROPERTIES = ["entity", "entities", "place"];

function received(event) {
  const type = event.data?.type;
  if (element && PROPERTIES.includes(type)) element[type] = event.data[type];
}

async function start() {
  await customElements.whenDefined(config.element);
  element = document.createElement(config.element);
  element.addEventListener("iki-select", selected);
  element.addEventListener("iki-more", () => send({ type: "more" }));
  element.addEventListener("iki-refine", refined);
  element.addEventListener("iki-open", opened);
  element.addEventListener("iki-run", ran);
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
