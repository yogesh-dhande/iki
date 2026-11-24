// Searching the site, offline: Pagefind's index is part of the build (`/pagefind/`), so
// nothing is asked of another origin. Results show under the header as the reader types,
// as a list of links they can Tab into; Escape closes them.

export interface Hit {
  url: string;
  title: string;
  /** Pagefind's excerpt: the page's own text, with the matches in <mark>. */
  excerpt: string;
}

export type Find = (query: string) => Promise<Hit[]>;

interface Pagefind {
  options(options: Record<string, unknown>): Promise<void>;
  search(query: string): Promise<{ results: { data(): Promise<PagefindData> }[] } | null>;
}

interface PagefindData {
  url: string;
  excerpt: string;
  meta: { title?: string };
}

const LIMIT = 8;

/** Pagefind's search, loaded the first time it's needed. */
export function pagefind(base = "/pagefind/pagefind.js"): Find {
  let loaded: Promise<Pagefind> | undefined;
  return async (query) => {
    loaded ??= (import(/* @vite-ignore */ base) as Promise<Pagefind>).then(async (module) => {
      await module.options({ excerptLength: 24 });
      return module;
    });
    const found = await (await loaded).search(query);
    const data = await Promise.all((found?.results ?? []).slice(0, LIMIT).map((r) => r.data()));
    return data.map((d) => ({ url: d.url, title: d.meta.title ?? d.url, excerpt: d.excerpt }));
  };
}

export function statusOf(query: string, hits: readonly Hit[]): string {
  if (!query) return "";
  if (hits.length === 0) return `Nothing matches “${query}”. Try fewer or other words.`;
  const shown =
    hits.length === LIMIT
      ? `The first ${String(LIMIT)} results`
      : `${String(hits.length)} result${hits.length === 1 ? "" : "s"}`;
  return `${shown} for “${query}”`;
}

function hitItem(document: Document, hit: Hit): HTMLLIElement {
  const item = document.createElement("li");
  const link = document.createElement("a");
  link.href = hit.url;
  const title = document.createElement("span");
  title.className = "hit-title";
  title.textContent = hit.title.replace(/ · Iki docs$/, "");
  const excerpt = document.createElement("span");
  excerpt.className = "hit-excerpt";
  // Pagefind's excerpt is the site's own text with <mark>s, from the index built with it.
  excerpt.innerHTML = hit.excerpt;
  link.append(title, excerpt);
  item.append(link);
  return item;
}

/** The header's search box and its results. */
export function startSearch(document: Document, find: Find = pagefind()): void {
  const form = document.querySelector<HTMLFormElement>("form[data-search]");
  const panel = document.querySelector<HTMLElement>("[data-search-panel]");
  const input = form?.querySelector<HTMLInputElement>("input[type=search]");
  const status = panel?.querySelector<HTMLElement>("[role=status]");
  const list = panel?.querySelector<HTMLOListElement>("ol");
  if (!form || !panel || !input || !status || !list) {
    return;
  }
  let asked = 0;
  // Closed with Escape: focus goes back to the box without opening the results again.
  let dismissed = false;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const close = () => {
    panel.hidden = true;
  };
  const run = async () => {
    const query = input.value.trim();
    const mine = (asked += 1);
    if (!query) {
      list.replaceChildren();
      status.textContent = "";
      close();
      return;
    }
    panel.hidden = false;
    let hits: Hit[];
    try {
      hits = await find(query);
    } catch {
      if (mine === asked) {
        list.replaceChildren();
        status.textContent = "Search isn't available here: it needs the built site (make docs).";
      }
      return;
    }
    if (mine !== asked) {
      return; // A later query answered first, or will.
    }
    list.replaceChildren(...hits.map((hit) => hitItem(document, hit)));
    status.textContent = statusOf(query, hits);
  };
  input.addEventListener("input", () => {
    dismissed = false;
    clearTimeout(timer);
    timer = setTimeout(() => void run(), 150);
  });
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    clearTimeout(timer);
    void run().then(() => list.querySelector("a")?.focus());
  });
  const escape = (event: KeyboardEvent) => {
    if (event.key === "Escape" && !panel.hidden) {
      close();
      dismissed = true;
      input.focus();
    }
  };
  input.addEventListener("keydown", escape);
  panel.addEventListener("keydown", escape);
  document.addEventListener("click", (event) => {
    const target = event.target as Node | null;
    if (target && !panel.contains(target) && !form.contains(target)) {
      close();
    }
  });
  input.addEventListener("blur", () => {
    dismissed = false;
  });
  input.addEventListener("focus", () => {
    if (
      !dismissed &&
      input.value.trim() &&
      list.childElementCount + (status.textContent ? 1 : 0) > 0
    ) {
      panel.hidden = false;
    }
  });
}
