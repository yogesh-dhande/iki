/* global CSS, Element, PerformanceObserver, location, performance, window */
// Installed before the app in every document, including sandboxed component frames.
(() => {
  const lookback = 500;
  const presses = new Map();
  let recent = [];

  function parent(element) {
    return element.assignedSlot ?? element.parentElement ?? element.getRootNode().host;
  }

  function contains(ancestor, element) {
    for (let node = element; node; node = parent(node)) {
      if (node === ancestor) return true;
    }
    return false;
  }

  function related(first, second) {
    return contains(first, second) || contains(second, first);
  }

  function describe(element) {
    const path = [];
    let control;
    for (let node = element; node; node = parent(node)) {
      if (path.length < 5) {
        path.unshift(
          node.localName +
            (node.id ? `#${CSS.escape(node.id)}` : "") +
            [...node.classList]
              .slice(0, 2)
              .map((name) => `.${CSS.escape(name)}`)
              .join(""),
        );
      }
      if (!control && node.matches("a[href], button, input, select, textarea, summary, [role]")) {
        control = node;
      }
    }
    const named = control ?? element;
    const labelledBy = (named.getAttribute("aria-labelledby") ?? "").trim().split(/\s+/);
    const role =
      named.getAttribute("role") ??
      { a: "link", button: "button", select: "combobox", textarea: "textbox" }[named.localName] ??
      null;
    const name = [
      labelledBy.map((id) => named.getRootNode().getElementById?.(id)?.textContent).join(" "),
      named.getAttribute("aria-label"),
      Array.from(named.labels ?? [], (label) => label.textContent).join(" "),
      named.getAttribute("alt"),
      named.textContent,
      named.getAttribute("title"),
    ].find((value) => value?.trim());
    return {
      tag: element.localName,
      role,
      name: name?.trim().replace(/\s+/g, " ").slice(0, 100) ?? "",
      path: path.join(" > "),
    };
  }

  function noteShifts(entries) {
    const cutoff = performance.now() - lookback;
    recent = recent.filter(({ entry }) => entry.startTime >= cutoff);
    for (const entry of entries) {
      for (const source of entry.sources) {
        const affected = source.node;
        const node = affected instanceof Element ? affected : affected?.parentElement;
        if (!node) continue;
        const shifted = { entry, source, node };
        if (entry.startTime >= cutoff) recent.push(shifted);
        for (const press of presses.values()) {
          if (entry.startTime >= press.down.at && related(press.target, node)) {
            press.shifts.push(shifted);
          }
        }
      }
    }
  }

  const observer = globalThis.PerformanceObserver?.supportedEntryTypes?.includes("layout-shift")
    ? new PerformanceObserver((list) => noteShifts(list.getEntries()))
    : null;
  observer?.observe({ type: "layout-shift", buffered: true });

  function pointer(event, target) {
    return { ...describe(target), at: event.timeStamp, x: event.clientX, y: event.clientY };
  }

  window.addEventListener(
    "pointerdown",
    (event) => {
      const target = event.composedPath()[0];
      if (!(target instanceof Element)) return;
      if (observer) noteShifts(observer.takeRecords());
      const down = pointer(event, target);
      presses.set(event.pointerId, {
        target,
        down,
        url: location.href,
        shifts: recent.filter(
          ({ entry, node }) => entry.startTime >= down.at - lookback && related(target, node),
        ),
      });
    },
    true,
  );

  window.addEventListener(
    "pointerup",
    (event) => {
      const press = presses.get(event.pointerId);
      if (!press) return;
      if (observer) noteShifts(observer.takeRecords());
      presses.delete(event.pointerId);
      const target = event.composedPath()[0];
      if (!(target instanceof Element)) return;

      const mismatched = !related(press.target, target);
      const shifts = press.shifts.filter(({ entry }) => entry.startTime <= event.timeStamp);
      if (!mismatched && !shifts.length) return;
      window
        .__ikiRecordClickStability({
          url: press.url,
          timeOrigin: performance.timeOrigin,
          down: press.down,
          up: pointer(event, target),
          mismatched,
          shifts: shifts.map(({ entry, source, node }) => ({
            at: entry.startTime,
            hadRecentInput: entry.hadRecentInput,
            value: entry.value,
            element: describe(node),
            from: source.previousRect.toJSON(),
            to: source.currentRect.toJSON(),
          })),
        })
        // Navigation can discard the binding's reply after Playwright has received the hit.
        .catch(() => undefined);
    },
    true,
  );
  window.addEventListener("pointercancel", (event) => presses.delete(event.pointerId), true);
})();
