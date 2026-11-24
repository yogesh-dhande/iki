// Marks the "On this page" link of the section being read: the last whose top has
// scrolled past a fifth of the window, or the first.
export function current(tops: readonly number[], line: number): number {
  let found = 0;
  tops.forEach((top, index) => {
    if (top <= line) {
      found = index;
    }
  });
  return found;
}

export function followSections(document: Document): void {
  const links = [...document.querySelectorAll<HTMLAnchorElement>("[data-toc] a[href^='#']")];
  const sections = links
    .map((link) => document.getElementById(decodeURIComponent(link.hash.slice(1))))
    .filter((section): section is HTMLElement => section !== null);
  if (sections.length !== links.length || links.length === 0) {
    return;
  }
  const view = document.defaultView;
  let frame = 0;
  const mark = () => {
    frame = 0;
    const tops = sections.map((section) => section.getBoundingClientRect().top);
    const at = current(tops, (view?.innerHeight ?? 800) / 5);
    links.forEach((link, index) => {
      if (index === at) {
        link.setAttribute("aria-current", "location");
      } else {
        link.removeAttribute("aria-current");
      }
    });
  };
  view?.addEventListener(
    "scroll",
    () => {
      frame ||= view.requestAnimationFrame(mark);
    },
    { passive: true },
  );
  mark();
}
