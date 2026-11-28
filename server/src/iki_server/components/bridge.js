// Iki's bridge, in a component's sandboxed frame (ADR 0084, ADR 0117, ADR 0170). It creates
// the component's element, gives it what the page sends (the entity whose page it is, the
// many entities its place shows, and where it is, with the actions it may offer), and
// passes on what the element tells the page, through the one port the page hands it. The
// page checks every message itself: the bridge shares the frame with the component's
// code, so it's a convenience, not a guard.
//
// It speaks @ikisci/component's frame protocol (ADR 0286), whose version is PROTOCOL: it
// connects only to a page that speaks it, and forwards exactly that package's EVENTS, as
// a test of the package holds it to.
const PROTOCOL = 1;
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
  if (typeof entity !== "string") return;
  const revision = event.detail?.revision;
  send(revision === undefined ? { type: "open", entity } : { type: "open", entity, revision });
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

// What the element may ask for, by its event's name, and how each is passed on.
const FORWARDED = {
  "iki-select": selected,
  "iki-more": () => send({ type: "more" }),
  "iki-refine": refined,
  "iki-open": opened,
  "iki-run": ran,
};

async function start() {
  await customElements.whenDefined(config.element);
  element = document.createElement(config.element);
  for (const [name, forward] of Object.entries(FORWARDED)) element.addEventListener(name, forward);
  document.body.append(element);
  const size = () => send({ type: "resize", height: document.documentElement.scrollHeight });
  new ResizeObserver(size).observe(document.body);
  send({ type: "ready", protocol: PROTOCOL });
}

function connected(event) {
  if (event.source !== window.parent || event.data?.type !== "iki-connect") return;
  // A page that speaks another version of the protocol gets no answer: it says the
  // component didn't start, rather than misreading what it sends.
  if (event.data.protocol !== PROTOCOL || event.ports.length !== 1) return;
  window.removeEventListener("message", connected);
  port = event.ports[0];
  port.onmessage = received;
  void start();
}

window.addEventListener("message", connected);
