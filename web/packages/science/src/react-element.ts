import type { ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";

/** React owns the light DOM of a complex viewer. Its host owns the element itself. */
export abstract class ReactElement extends HTMLElement {
  private root: Root | null = null;

  connectedCallback() {
    this.root ??= createRoot(this);
    this.invalidate();
  }

  disconnectedCallback() {
    // The host may be committing its own React tree, or moving us within the document.
    queueMicrotask(() => {
      if (this.isConnected) return;
      this.root?.unmount();
      this.root = null;
    });
  }

  protected invalidate() {
    this.root?.render(this.renderView());
  }

  protected abstract renderView(): ReactNode;
}
