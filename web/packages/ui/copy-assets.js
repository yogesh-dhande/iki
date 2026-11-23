// Django serves these same files directly, including pages without JavaScript.
import { cp, mkdir } from "node:fs/promises";
import { URL } from "node:url";

const dist = new URL("./dist/", import.meta.url);
const assets = new URL("../../../server/src/iki_server/web/static/iki/", import.meta.url);
await mkdir(dist, { recursive: true });
for (const name of [
  "button.css",
  "page-header.css",
  "sidebar.css",
  "tokens.css",
  "fonts.css",
  "fonts",
]) {
  await cp(new URL(name, assets), new URL(name, dist), { recursive: true });
}
