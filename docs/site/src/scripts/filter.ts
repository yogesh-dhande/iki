// Narrows a list as the reader types or picks a status: each row's words and standing are
// in its data attributes, so nothing is fetched.
export function matches(text: string, standing: string, query: string, status: string): boolean {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  return (!status || standing === status) && words.every((word) => text.includes(word));
}

export function startFilter(list: HTMLElement): void {
  const form = list.querySelector<HTMLFormElement>("form[data-filter]");
  const count = list.querySelector<HTMLElement>("[data-count]");
  const rows = [...list.querySelectorAll<HTMLElement>("li[data-standing]")];
  if (!form || !count) {
    return;
  }
  const apply = () => {
    const query = (form.elements.namedItem("q") as HTMLInputElement | null)?.value ?? "";
    const status = (form.elements.namedItem("status") as HTMLSelectElement | null)?.value ?? "";
    let shown = 0;
    for (const row of rows) {
      const show = matches(row.dataset["text"] ?? "", row.dataset["standing"] ?? "", query, status);
      row.hidden = !show;
      shown += show ? 1 : 0;
    }
    count.textContent =
      query || status
        ? shown
          ? `${String(shown)} of ${String(rows.length)} decisions`
          : "No decision matches. Try fewer words, or any status."
        : "";
  };
  form.addEventListener("input", apply);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
  });
  apply();
}
