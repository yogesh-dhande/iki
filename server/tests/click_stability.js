/* global CSS, Element, PerformanceObserver, location, performance, window */
// Installed before the app in every document, including sandboxed component frames.
(() => {
  const lookback = 500;
  const interactive = [
    "a[href]",
    "button",
    "input:not([type=hidden])",
    "label",
    "select",
    "textarea",
    "summary",
    "[contenteditable]:not([contenteditable=false])",
    "[tabindex]:not([tabindex='-1'])",
    ..."button checkbox combobox gridcell link menuitem menuitemcheckbox menuitemradio option radio searchbox slider spinbutton switch tab textbox treeitem"
      .split(" ")
      .map((role) => `[role~=${role}]`),
  ].join(",");
  const presses = new Map();
  let recent = [];
  let expectedResize;
  let resizeFrames = [];

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

  function controlFor(element) {
    for (let node = element; node; node = parent(node)) {
      if (node.matches(interactive)) return node;
    }
    return null;
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
          if (
            entry.startTime >= press.down.at - lookback &&
            related(press.control ?? press.target, node)
          ) {
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

  if (observer) {
    const viewport = () => ({ width: window.innerWidth, height: window.innerHeight });
    window.__ikiMarkTestResize = (requested) => {
      expectedResize = { requested, before: viewport(), at: performance.now() };
    };
    window.addEventListener("resize", () => {
      if (!expectedResize) return;
      const at = performance.now();
      const { requested, before, at: requestedAt } = expectedResize;
      expectedResize = undefined;
      const after = viewport();
      if (
        at - requestedAt > lookback ||
        (before.width === after.width && before.height === after.height) ||
        (window === window.top &&
          (after.width !== requested.width || after.height !== requested.height))
      ) {
        return;
      }
      const frame = { resize: { at, requestedAt, requested, before, after }, until: Infinity };
      resizeFrames = resizeFrames.filter(({ until }) => until >= at - lookback);
      resizeFrames.push(frame);
      // The resize event is delivered before callbacks for the first affected frame.
      window.requestAnimationFrame(() => {
        window.requestAnimationFrame(() => {
          window.requestAnimationFrame(() => {
            frame.until = performance.now();
          });
        });
      });
    });
  }

  function inRect({ x, y }, rect) {
    return x >= rect.left && x < rect.right && y >= rect.top && y < rect.bottom;
  }

  function describeShifts(press, upAt) {
    const shifts = press.shifts.filter(({ entry }) => entry.startTime <= upAt);
    const controlSources = new Map();
    for (const { entry, source, node } of shifts) {
      if (node === press.control) controlSources.set(entry, source);
    }
    return shifts.map(({ entry, source, node }) => {
      const at = entry.startTime;
      const phase = at < press.down.at ? "pre-press" : "in-press";
      const testResize =
        resizeFrames.findLast(({ resize, until }) => resize.at <= at && at < until)?.resize ?? null;
      // Chromium supplies viewport-clipped rectangles for its attributed nodes. A related
      // node's rectangles alone cannot say whether the interactive element moved.
      const control = controlSources.get(entry);
      const hitAreaChanged = control
        ? inRect(press.down, control.previousRect) !== inRect(press.down, control.currentRect)
        : null;
      let suppressedBy = null;
      if (phase === "pre-press") {
        if (testResize) suppressedBy = "test-resize";
        else if (hitAreaChanged === null) suppressedBy = "hit-area-unattributed";
        else if (!hitAreaChanged) suppressedBy = "hit-area-unchanged";
      }
      return {
        at,
        phase,
        hadRecentInput: entry.hadRecentInput,
        value: entry.value,
        element: describe(node),
        from: source.previousRect.toJSON(),
        to: source.currentRect.toJSON(),
        testResize,
        hitAreaChanged,
        suppressedBy,
      };
    });
  }

  function pointer(event, target) {
    return { ...describe(target), at: event.timeStamp, x: event.clientX, y: event.clientY };
  }

  window.addEventListener(
    "pointerdown",
    (event) => {
      const path = event.composedPath();
      const target = path[0];
      if (!(target instanceof Element)) return;
      if (observer) noteShifts(observer.takeRecords());
      const down = pointer(event, target);
      const control = observer ? controlFor(target) : null;
      presses.set(event.pointerId, {
        target,
        path,
        control,
        down,
        url: location.href,
        shifts: recent.filter(
          ({ entry, node }) =>
            entry.startTime >= down.at - lookback && related(control ?? target, node),
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
      const path = event.composedPath();
      const target = path[0];
      if (!(target instanceof Element)) return;

      const mismatched = !path.includes(press.target) && !press.path.includes(target);
      if (!mismatched && !press.shifts.length) return;
      const shifts = describeShifts(press, event.timeStamp);
      if (!mismatched && !shifts.length) return;
      window
        .__ikiRecordClickStability({
          url: press.url,
          timeOrigin: performance.timeOrigin,
          down: press.down,
          up: pointer(event, target),
          mismatched,
          movingTarget: shifts.some(({ suppressedBy }) => suppressedBy === null),
          control: press.control ? describe(press.control) : null,
          shifts,
        })
        // Navigation can discard the binding's reply after Playwright has received the hit.
        .catch(() => undefined);
    },
    true,
  );
  window.addEventListener("pointercancel", (event) => presses.delete(event.pointerId), true);
})();
