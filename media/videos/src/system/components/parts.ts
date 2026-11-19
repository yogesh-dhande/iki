// The named parts of a scene, which beats' steps act on.
import type { Node } from "@motion-canvas/2d";

export class Parts {
  private readonly nodes = new Map<string, Node>();

  /** A ref callback that files the node under `name`. */
  ref(name: string): (node: Node) => void {
    return (node: Node) => {
      this.nodes.set(name, node);
    };
  }

  get(name: string): Node {
    const node = this.nodes.get(name);
    if (!node)
      throw new Error(`No part "${name}"; there are ${[...this.nodes.keys()].join(", ")}.`);
    return node;
  }

  names(): string[] {
    return [...this.nodes.keys()];
  }
}
