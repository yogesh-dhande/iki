// The worked plate's two controls: choosing a well shows its reading, and the toggle
// switches the figure between the fit with every sample well and the fit without the
// excluded one. Both fits are in the page already; this only shows one or the other.
export function startPlateAssay(figure: HTMLElement): void {
  const toggle = figure.querySelector<HTMLButtonElement>("[data-toggle]");
  const wells = [...figure.querySelectorAll<HTMLButtonElement>("button[data-well]")];
  const show = (name: string, value: string) => {
    const target = figure.querySelector<HTMLElement>(`[data-show="${name}"]`);
    if (target) target.textContent = value;
  };
  const excluded = () => figure.hasAttribute("data-excluded");
  const select = (well: HTMLButtonElement) => {
    for (const other of wells) {
      other.setAttribute("aria-pressed", String(other === well));
    }
    const out = excluded() && well.hasAttribute("data-excludable");
    show("well", well.dataset["well"] ?? "");
    show("role", out ? `${well.dataset["role"] ?? ""}, excluded` : (well.dataset["role"] ?? ""));
    show("detail", well.dataset["detail"] ?? "");
    show("reading", well.dataset["reading"] ?? "");
    figure.dataset["selected"] = well.hasAttribute("data-excludable") ? "excludable" : "";
  };
  for (const well of wells) {
    well.addEventListener("click", () => {
      select(well);
    });
  }
  const excludable = wells.find((well) => well.hasAttribute("data-excludable"));
  toggle?.addEventListener("click", () => {
    const now = !excluded();
    figure.toggleAttribute("data-excluded", now);
    toggle.setAttribute("aria-pressed", String(now));
    if (excludable) select(excludable);
  });
  const pressed = wells.find((well) => well.getAttribute("aria-pressed") === "true");
  if (pressed) select(pressed);
}
