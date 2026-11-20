// Loads the bundled fonts into the page before anything is drawn. Every video's project
// imports this module, and its top-level await holds the project back until the fonts are
// in, in the renderer and in the editor alike.
import { fontFaces } from "../theme/fonts.ts";

const files = import.meta.glob<string>("../../../fonts/*/*.woff2", {
  query: "?url",
  import: "default",
  eager: true,
});

await Promise.all(
  fontFaces.map(async ({ family, weight, file }) => {
    const url = files[`../../../fonts/${file}`];
    if (!url) throw new Error(`Missing font: fonts/${file}`);
    const face = new FontFace(family, `url(${url})`, { weight: String(weight) });
    document.fonts.add(await face.load());
  }),
);
