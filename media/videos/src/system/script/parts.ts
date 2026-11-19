// The parts of a scene that steps can show, hide, emphasize or dim, by name. Each layout
// names its parts the same way here (for the checks, in Node) and when it builds them
// (layouts/, in the browser); a test holds the two to each other.
import type { Content, Scene } from "./schema.ts";

function contentParts(prefix: string, content: Content): string[] {
  if ("record" in content) {
    return content.record.fields.map((field) => `${prefix}field:${field.label}`);
  }
  return [];
}

/** Every part a scene's layout builds, by name. */
export function partsOf(scene: Scene): string[] {
  switch (scene.layout) {
    case "title":
      return ["brand", "eyebrow", "title", ...(scene.slots.subtitle ? ["subtitle"] : [])];
    case "closing":
      return ["statement", ...(scene.slots.next ? ["next"] : []), "brand"];
    case "focus":
      return [
        ...(scene.slots.heading ? ["heading"] : []),
        "object",
        ...contentParts("", scene.slots.object),
      ];
    case "gallery":
      return [
        ...(scene.slots.heading ? ["heading"] : []),
        ...scene.slots.items.flatMap((item, index) => [
          `item:${String(index + 1)}`,
          ...contentParts(`item:${String(index + 1)}.`, item),
        ]),
      ];
    case "split":
      return [
        ...(scene.slots.heading ? ["heading"] : []),
        "left",
        "right",
        ...contentParts("left.", scene.slots.left.content),
        ...contentParts("right.", scene.slots.right.content),
        ...(scene.slots.link ? ["link"] : []),
        ...(scene.slots.note ? ["note"] : []),
      ];
    case "record-inset":
      return [
        "record",
        ...scene.slots.record.fields.map((field) => `field:${field.label}`),
        "inset",
        ...(scene.slots.link ? ["link"] : []),
      ];
    case "timeline":
      return [
        ...(scene.slots.heading ? ["heading"] : []),
        "subject",
        "axis",
        ...scene.slots.revisions.flatMap((revision) => [
          `revision:${String(revision.number)}`,
          ...(revision.signature ? [`signature:${String(revision.number)}`] : []),
        ]),
      ];
    case "graph":
      return [
        ...(scene.slots.heading ? ["heading"] : []),
        ...scene.slots.nodes.flatMap((node) => [
          `node:${node.id}`,
          ...(node.signature ? [`signature:${node.id}`] : []),
        ]),
        ...scene.slots.edges.map((edge) => `edge:${edge.from}>${edge.to}`),
      ];
  }
}
