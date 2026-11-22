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
});
