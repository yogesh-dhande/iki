/** In-memory drawing API from RDKit.js MinimalLib. The builder supplies its loader and bytes. */
interface DrawnMolecule {
  is_valid(): boolean;
  get_svg_with_highlights(options: string): string;
  delete(): void;
}

interface RDKit {
  get_mol(smiles: string): DrawnMolecule | null;
}

declare const RDKIT_WASM_GZIP_BASE64: string;
declare function initRDKitModule(options: {
  instantiateWasm(
    imports: WebAssembly.Imports,
    ready: (instance: WebAssembly.Instance) => void,
  ): void;
}): Promise<RDKit>;

let loading: Promise<RDKit> | undefined;

async function loadRDKit(): Promise<RDKit> {
  const packed = Uint8Array.from(atob(RDKIT_WASM_GZIP_BASE64), (c) => c.charCodeAt(0));
  const bytes = new Blob([packed]).stream().pipeThrough(new DecompressionStream("gzip"));
  const module = await WebAssembly.compile(await new Response(bytes).arrayBuffer());
  return initRDKitModule({
    instantiateWasm(imports, ready) {
      ready(new WebAssembly.Instance(module, imports));
    },
  });
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

export interface ChemicalDepictionData {
  smiles: string;
  name?: string;
  label?: string;
}

export class ChemicalDepiction extends HTMLElement {
  #data: ChemicalDepictionData | null = null;
  readonly #root = this.attachShadow({ mode: "open" });
  #generation = 0;
  #url: string | null = null;

  get data(): ChemicalDepictionData | null {
    return this.#data;
  }

  set data(value: ChemicalDepictionData | null) {
    this.#data = value;
    if (this.isConnected) void this.#draw();
  }

  connectedCallback(): void {
    void this.#draw();
  }

  disconnectedCallback(): void {
    ++this.#generation;
    this.#releaseImage();
  }

  #releaseImage(): void {
    if (this.#url) URL.revokeObjectURL(this.#url);
    this.#url = null;
  }

  async #draw(): Promise<void> {
    const generation = ++this.#generation;
    this.#releaseImage();
    const data = this.#data;
    if (!data) {
      this.#root.replaceChildren();
      return;
    }
    const style = document.createElement("style");
    style.textContent = STYLE;
    const status = document.createElement("p");
    status.setAttribute("role", "status");
    status.textContent = "Drawing…";
    this.#root.replaceChildren(style, status);
    try {
      const rdkit = await (loading ??= loadRDKit().catch((error: unknown) => {
        loading = undefined;
        throw error;
      }));
      if (generation !== this.#generation) return;
      const mol = data.smiles ? rdkit.get_mol(data.smiles) : null;
      let svg: string;
      try {
        if (!mol?.is_valid()) throw new Error("RDKit can't read its SMILES");
        svg = mol.get_svg_with_highlights(
          JSON.stringify({ width: 480, height: 360, addStereoAnnotation: true }),
        );
      } finally {
        mol?.delete();
      }
      const figure = document.createElement("figure");
      const image = document.createElement("img");
      image.alt = `The structure of ${data.name ?? data.label ?? "the compound"}: ${data.smiles}`;
      this.#url = URL.createObjectURL(new Blob([svg], { type: "image/svg+xml" }));
      image.src = this.#url;
      const caption = document.createElement("figcaption");
      const code = document.createElement("code");
      code.textContent = data.smiles;
      caption.append(code);
      figure.append(image, caption);
      this.#root.replaceChildren(style, figure);
      this.dispatchEvent(
        new CustomEvent("depiction-ready", {
          detail: { smiles: data.smiles },
          bubbles: true,
          composed: true,
        }),
      );
    } catch (error) {
      if (generation !== this.#generation) return;
      const message = `${data.label ?? data.name ?? "It"} can't be drawn: ${error instanceof Error ? error.message : String(error)}.`;
      status.setAttribute("role", "alert");
      status.textContent = message;
      this.dispatchEvent(
        new CustomEvent("depiction-error", { detail: message, bubbles: true, composed: true }),
      );
    }
  }
}

if (!customElements.get("iki-chemical-depiction"))
  customElements.define("iki-chemical-depiction", ChemicalDepiction);

declare global {
  interface HTMLElementEventMap {
    "depiction-ready": CustomEvent<{ smiles: string }>;
    "depiction-error": CustomEvent<string>;
  }
  interface HTMLElementTagNameMap {
    "iki-chemical-depiction": ChemicalDepiction;
  }
}
