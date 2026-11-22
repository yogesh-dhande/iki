// Progressive enhancement for server-rendered pages. Every page works without it.
// Runs under a strict CSP with Trusted Types: no innerHTML, eval or inline handlers.
"use strict";

// Times on these pages are in the viewer's time zone, as in the web app, which sets this
// cookie too (iki_server.web.timezones). Without it, a page shows UTC and says so.
(() => {
  const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  if (!zone || !/^[A-Za-z0-9_+\-/]+$/.test(zone)) return;
  const secure = window.location.protocol === "https:" ? "; secure" : "";
  document.cookie = `iki_time_zone=${zone}; path=/; max-age=31536000; samesite=lax${secure}`;
})();

document.addEventListener("DOMContentLoaded", () => {
  // Setup links carry their one-time code in the URL fragment, which browsers
  // never send to servers. Move it into the form, drop it from the address bar
  // and history, and continue.
  const tokenForm = document.getElementById("setup-token-form");
  const code = window.location.hash.slice(1);
  if (tokenForm && code) {
    history.replaceState(null, "", window.location.pathname);
    const input = tokenForm.querySelector("input[name=token]");
    if (input && !tokenForm.querySelector(".field-error")) {
      input.value = code;
      tokenForm.requestSubmit();
    }
  }

  // Busy state: disable submit buttons after the first click, so a slow
  // redirect to the identity provider isn't submitted twice.
  for (const form of document.querySelectorAll("form[data-busy-form]")) {
    form.addEventListener("submit", (event) => {
      if (form.dataset.busy === "true") {
        event.preventDefault();
        return;
      }
      form.dataset.busy = "true";
      const button = event.submitter || form.querySelector("button[type=submit]");
      if (button) {
        button.setAttribute("aria-disabled", "true");
        button.setAttribute("aria-busy", "true");
        if (button.dataset.busyText) button.textContent = button.dataset.busyText;
      }
    });
  }

  // Copy buttons: data-copy names the element whose text is copied.
  for (const button of document.querySelectorAll("button[data-copy]")) {
    button.addEventListener("click", async () => {
      const source = document.getElementById(button.dataset.copy);
      if (!source || !navigator.clipboard) return;
      const label = button.textContent;
      try {
        await navigator.clipboard.writeText(source.textContent.trim());
        button.textContent = "Copied";
      } catch {
        button.textContent = "Copy failed";
      }
      setTimeout(() => {
        button.textContent = label;
      }, 2000);
    });
  }

  // The admin area's menu: on a narrow screen its pages fold under a button. Without
  // this, the button stays hidden and the pages stay listed.
  const menu = document.querySelector("button[data-admin-menu]");
  if (menu) {
    menu.setAttribute("aria-expanded", "false");
    menu.hidden = false;
    menu.addEventListener("click", () => {
      const open = menu.getAttribute("aria-expanded") === "true";
      menu.setAttribute("aria-expanded", open ? "false" : "true");
    });
  }

  // Move focus to an error summary, so keyboard and screen reader users land on it.
  const summary = document.querySelector("[data-focus-on-load]");
  if (summary) {
    summary.setAttribute("tabindex", "-1");
    summary.focus();
  }

  const sidebar = document.querySelector("[data-sidebar]");
  if (sidebar) setUpSidebar(sidebar);
});

// --- The sidebar (ADR 0176), as the web app's sidebar.tsx works -----------------------

// Below this width the sidebar is a drawer, opened from the menu button (iki.css).
const DRAWER = window.matchMedia("(max-width: 56rem)");
// Whether the Entities section is open, as the reader left it (iki_server.web.navigation).
const ENTITIES_COOKIE = "iki_entities";

function setUpSidebar(sidebar) {
  // The shortcut as this computer's keyboard says it.
  if (/Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent)) {
    for (const kbd of sidebar.querySelectorAll("[data-shortcut]")) kbd.textContent = "⌘K";
  }

  // Search everything: the web app's palette, which this page loads with the button, and
  // which binds ⌘K (Ctrl+K) itself.
  sidebar.querySelector("[data-search]")?.addEventListener("click", () => {
    window.dispatchEvent(new window.CustomEvent("iki:search"));
  });

  const toggle = sidebar.querySelector("[data-entities-toggle]");
  const types = sidebar.querySelector("[data-entity-types]");
  toggle?.addEventListener("click", () => {
    const open = toggle.getAttribute("aria-expanded") !== "true";
    toggle.setAttribute("aria-expanded", String(open));
    types.hidden = !open;
    const secure = window.location.protocol === "https:" ? "; secure" : "";
    document.cookie = `${ENTITIES_COOKIE}=${open ? "open" : "closed"}; path=/; max-age=31536000; samesite=lax${secure}`;
  });
  if (types) showEntityTypes(types);

  setUpAccount(sidebar);
  setUpDrawer(sidebar);
}

// The types of entity with records the reader can read, with how many of each (ADR 0176).
// Where counting would be slow, every type is listed without a count; if the counts can't
// be read, All entities alone.
async function showEntityTypes(list) {
  let types;
  let total;
  try {
    const counted = await window.fetch("/api/v1/entities/counts", {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    });
    if (!counted.ok) return;
    ({ types, total } = await counted.json());
  } catch {
    return;
  }
  const all = list.querySelector("a");
  if (total !== null) all?.append(count(total));
  for (const t of types) {
    const link = document.createElement("a");
    link.className = "nav-item";
    link.href = `/entities/types/${encodeURI(t.type)}/`;
    const label = document.createElement("span");
    label.className = "nav-label";
    label.textContent = t.label;
    link.append(label);
    if (t.count !== null) link.append(count(t.count));
    if (window.location.pathname.startsWith(link.pathname)) {
      all?.removeAttribute("aria-current");
      link.setAttribute("aria-current", "page");
    }
    list.append(link);
  }
}

// How many: after a space, which a flex row doesn't draw but a screen reader reads.
function count(n) {
  const span = document.createElement("span");
  span.className = "nav-count";
  span.textContent = n.toLocaleString();
  const both = document.createDocumentFragment();
  both.append(" ", span);
  return both;
}

// The account menu: opens over the sidebar, and closes on Escape, a click elsewhere, or
// moving on from it.
function setUpAccount(sidebar) {
  const button = sidebar.querySelector("[data-account-toggle]");
  const menu = sidebar.querySelector("[data-account-menu]");
  if (!button || !menu) return;
  const account = button.parentElement;
  const show = (open) => {
    button.setAttribute("aria-expanded", String(open));
    menu.hidden = !open;
  };
  button.addEventListener("click", () => {
    const open = menu.hidden;
    show(open);
    if (open) menu.querySelector("a, button")?.focus();
  });
  account.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !menu.hidden) {
      event.stopPropagation();
      show(false);
      button.focus();
    }
  });
  account.addEventListener("focusout", (event) => {
    if (!account.contains(event.relatedTarget)) show(false);
  });
  document.addEventListener("click", (event) => {
    if (!account.contains(event.target)) show(false);
  });
}

// On a narrow screen: a drawer over the page, which is out of reach until it closes.
function setUpDrawer(sidebar) {
  const opener = document.querySelector("[data-drawer-open]");
  const backdrop = document.querySelector(".sidebar-backdrop");
  if (!opener) return;
  const outside = () =>
    [...document.body.children].filter(
      (el) => el !== sidebar && el !== backdrop && el.tagName !== "SCRIPT",
    );
  const close = (refocus) => {
    if (sidebar.dataset.open !== "true") return;
    delete sidebar.dataset.open;
    for (const name of ["role", "aria-modal", "aria-label"]) sidebar.removeAttribute(name);
    backdrop.hidden = true;
    opener.setAttribute("aria-expanded", "false");
    for (const el of outside()) el.inert = false;
    if (refocus) opener.focus();
  };
  opener.addEventListener("click", () => {
    sidebar.dataset.open = "true";
    sidebar.setAttribute("role", "dialog");
    sidebar.setAttribute("aria-modal", "true");
    sidebar.setAttribute("aria-label", "Menu");
    backdrop.hidden = false;
    opener.setAttribute("aria-expanded", "true");
    for (const el of outside()) el.inert = true;
    sidebar.querySelector("[data-drawer-close]")?.focus();
  });
  for (const el of document.querySelectorAll("[data-drawer-close]")) {
    el.addEventListener("click", () => close(true));
  }
  sidebar.addEventListener("keydown", (event) => {
    if (event.key === "Escape") close(true);
  });
  DRAWER.addEventListener("change", (event) => {
    if (!event.matches) close(false);
  });
}
