// On a phone, the Menu button shows and hides the site's navigation above the page.
export function startMenu(root: Document): void {
  const button = root.querySelector<HTMLButtonElement>(".menu-button");
  const nav = root.getElementById("site-nav");
  if (!button || !nav) return;
  const show = (open: boolean) => {
    button.setAttribute("aria-expanded", String(open));
    nav.classList.toggle("is-open", open);
  };
  button.addEventListener("click", () => {
    show(button.getAttribute("aria-expanded") !== "true");
  });
  root.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && button.getAttribute("aria-expanded") === "true") {
      show(false);
      button.focus();
    }
  });
}
