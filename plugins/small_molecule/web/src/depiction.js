// The small_molecule plugin's Structure tab: a compound's 2D depiction, drawn in the
// browser by RDKit's own WebAssembly build (RDKit.js, the same release as the plugin's
// actions run), from the compound's SMILES.
//
// It runs in Iki's sandboxed component frame (ADR 0084), which allows no network: the
// WebAssembly module is bundled into this file, gzipped, and RDKit is handed its bytes,
// so it never fetches anything. The drawing is shown as an image, so nothing in it runs.
//
// `build.js` puts this after RDKit.js's loader (which defines `initRDKitModule`) and
// the WebAssembly bytes (`RDKIT_WASM_GZIP_BASE64`).

/* global initRDKitModule, RDKIT_WASM_GZIP_BASE64 */

/** @type {Promise<any> | undefined} */
let loading;

/** RDKit, loaded once for the frame, from the bundled bytes. */
function rdkit() {
  loading ??= (async () => {
    const packed = Uint8Array.from(atob(RDKIT_WASM_GZIP_BASE64), (c) => c.charCodeAt(0));
    const stream = new Blob([packed]).stream().pipeThrough(new DecompressionStream("gzip"));
    const wasm = await new Response(stream).arrayBuffer();
    // Handed the module itself, RDKit.js never fetches it.
    return initRDKitModule({
      instantiateWasm(imports, done) {
        void WebAssembly.instantiate(wasm, imports).then(({ instance }) => done(instance));
        return {};
      },
    });
  })();
  return loading;
}

const STYLE = `
:host { display: block; color: CanvasText; font: 14px/1.4 system-ui, sans-serif; }
figure { margin: 0; display: grid; gap: 8px; justify-items: start; }
img { width: min(100%, 480px); height: auto; aspect-ratio: 4 / 3; background: #fff;
  border: 1px solid color-mix(in srgb, CanvasText 15%, transparent); border-radius: 6px; }
figcaption { overflow-wrap: anywhere; }
code { font: 13px ui-monospace, monospace; }
p[role="alert"] { margin: 0; }
`;

class Depiction extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
  }

  /** The compound whose page it's on, as the person may read it. */
  set entity(entity) {
    void this.draw(entity);
  }

  async draw(entity) {
    const smiles = entity?.values?.smiles ?? "";
    const style = document.createElement("style");
    style.textContent = STYLE;
    const status = document.createElement("p");
    status.textContent = "Drawing…";
    this.shadowRoot.replaceChildren(style, status);
    let svg;
    try {
      const RDKit = await rdkit();
      const mol = smiles ? RDKit.get_mol(smiles) : null;
      if (!mol?.is_valid()) {
        mol?.delete();
        throw new Error("RDKit can't read its SMILES");
      }
      try {
        svg = mol.get_svg_with_highlights(
          JSON.stringify({ width: 480, height: 360, addStereoAnnotation: true }),
        );
      } finally {
        mol.delete();
      }
    } catch (error) {
      status.setAttribute("role", "alert");
      status.textContent = `${entity?.hrid ?? "It"} can't be drawn: ${String(error?.message ?? error)}.`;
      return;
    }
    const figure = document.createElement("figure");
    const image = document.createElement("img");
    image.alt = `The structure of ${entity.values.name || entity.hrid}: ${smiles}`;
    const url = URL.createObjectURL(new Blob([svg], { type: "image/svg+xml" }));
    image.addEventListener("load", () => URL.revokeObjectURL(url), { once: true });
    image.src = url;
    const caption = document.createElement("figcaption");
    const code = document.createElement("code");
    code.textContent = smiles;
    caption.append(code);
    figure.append(image, caption);
    this.shadowRoot.replaceChildren(style, figure);
  }
}

customElements.define("small-molecule-depiction", Depiction);
