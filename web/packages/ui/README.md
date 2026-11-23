# @ikisci/ui

Shared design tokens, typography and controls for web applications. The CSS and
JavaScript entries can be imported independently. The controls include TypeScript
declarations.

## Button

```js
import "@ikisci/ui/tokens.css";
import "@ikisci/ui/button.css";
import "@ikisci/ui/button";

const button = document.createElement("iki-button");
button.data = { text: "Save", variant: "primary" };
button.addEventListener("iki-press", () => console.log("Save requested"));
document.body.append(button);
```

Set `.data` to an object with `text` and optional `variant`, `type`, `label`,
`describedBy`, `busy`, `busyText` and `disabled`. The element contains a native
button and supports keyboard activation. Set `type: "submit"` inside a form to use
native form submission. The cancelable `iki-press` event bubbles and is composed.
Calling `preventDefault()` on that event cancels the button's default action.
[Button story](stories/button.html)

## Tokens and Inter

Import `@ikisci/ui/tokens.css` for the light and dark palettes, type scale, control
heights, spacing dimensions and focus treatment. The theme follows
`prefers-color-scheme`. For example, set a panel's background to `var(--surface)`
and its text to `var(--text)`. Import `@ikisci/ui/fonts.css` to load the self-hosted
Inter variable font and apply `font-family: var(--font)`. The release includes the
font's OFL text alongside its WOFF2 files. [Tokens and Inter story](stories/tokens.html)

## Next.js

Load the CSS from an application stylesheet or layout. In a Client Component, import
`@ikisci/ui/button` inside `useEffect`, then create the element or assign its data.
This allows the page's initial markup to render on the server. [Next.js Client Components](https://nextjs.org/docs/app/getting-started/server-and-client-components)

## Run the stories

From the repository root run `pnpm install --frozen-lockfile` and
`pnpm --filter @ikisci/ui build`. Serve `web/packages` with
`python3 -m http.server --directory web/packages 8000` and open
`/ui/stories/button.html` or `/ui/stories/tokens.html`. Each entry's build checks a
gzip byte budget, including the font files referenced by the font stylesheet.
