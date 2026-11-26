// The reader's colour theme: their system's, unless they chose light or dark here. The
// choice is kept in this browser only; the page's first script (Base.astro) applies it
// before the page is drawn, so it never flashes the other theme.
export type Choice = "system" | "light" | "dark";

export const KEY = "iki-docs-theme";

export function stored(storage: Pick<Storage, "getItem"> | undefined): Choice {
  try {
    const value = storage?.getItem(KEY);
    return value === "light" || value === "dark" ? value : "system";
  } catch {
    return "system";
  }
}

export function apply(root: HTMLElement, choice: Choice): void {
  if (choice === "system") {
    delete root.dataset["theme"];
  } else {
    root.dataset["theme"] = choice;
  }
}

function storage(): Storage | undefined {
  try {
    return window.localStorage;
  } catch {
    return undefined;
  }
}

/** The theme buttons: each says whether it's the current choice, and choosing one applies
 * it, keeps it, and tells the viewers' frames. */
export function startThemeSwitch(document: Document): void {
  const group = document.querySelector<HTMLElement>("[data-theme-switch]");
  if (!group) {
    return;
  }
  const buttons = [...group.querySelectorAll<HTMLButtonElement>("button[data-choice]")];
  const show = (choice: Choice) => {
    for (const button of buttons) {
      button.setAttribute("aria-pressed", String(button.dataset["choice"] === choice));
    }
  };
  show(stored(storage()));
  for (const button of buttons) {
    button.addEventListener("click", () => {
      const choice = (button.dataset["choice"] ?? "system") as Choice;
      apply(document.documentElement, choice);
      try {
        if (choice === "system") {
          storage()?.removeItem(KEY);
        } else {
          storage()?.setItem(KEY, choice);
        }
      } catch {
        // Storage refused (a private window): the choice holds for this page only.
      }
      show(choice);
    });
  }
}
