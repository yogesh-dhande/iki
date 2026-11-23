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

## Page header

Import `@ikisci/ui/page-header`, `@ikisci/ui/page-header.css`, `@ikisci/ui/button.css`
and the tokens. Assign `.data` on `<iki-page-header>` with the required string
`title`. The optional strings `description`, `eyebrow` and `badge` sit beside the
title. Set `about` to a link `{label, href}` to add a details button. A truncated
description also opens in the accessible details dialog. `ancestors` is an array of
links in breadcrumb order. `actions` takes objects with string `id` and `label`.
Activating an action sends a bubbling, composed `iki-action` with `detail.id`.
The action row moves below the title on a phone. Custom renderers can use the
element’s stable light-DOM `areas` for rich content. [Page header story](stories/page-header.html)

## Sidebar

Import `@ikisci/ui/sidebar`, `@ikisci/ui/sidebar.css` and the tokens. Set
`<iki-sidebar>`’s `.data` to `{label, sections}`. The label names its navigation
landmark. Each section has `links`, an optional group `label`, optional heading
link `action` and an optional Boolean `gap`. To make a group collapsible, supply
`disclosure: {id, open}` with a document-unique ID. A toggle sends a bubbling,
composed `iki-toggle` with `detail: {id, open}`.

Each link has string `label` and `href`. Optional fields include `current`
and `quiet` flags, a text `qualifier`, numeric `count`, or a
`badge: {text, description}`. Links can have an `icon` array of SVG path
strings (24 × 24 view box) or a `mark: {shape, swatch}` where shape is
`dot` or `square` and swatch is 1–8. Disclosures also accept icon paths.
A plain activation of a sidebar or breadcrumb link dispatches a bubbling,
composed `iki-navigate` with `detail.href`. A router may cancel the event
and handle navigation. Modified clicks retain native browser behavior.
[Sidebar story](stories/sidebar.html)

## Tokens and Inter

Import `@ikisci/ui/tokens.css` for the light and dark palettes, type scale, control
heights, spacing dimensions and focus treatment. The theme follows
`prefers-color-scheme`. Set `data-theme="light"` or `data-theme="dark"` on the
document’s `<html>` element to choose a theme explicitly. Remove the attribute to
follow the device again. For example, set a panel's background to `var(--surface)`
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
