/**
 * An entity type's color (ADR 0176): a small dot before its name, wherever types are
 * listed (the sidebar, the lists' type pills and Type column, search everything). Types
 * have no color of their own to choose, so each takes one of the eight project swatches
 * from its name, as a project does from its ID: the same for a type on every page and for
 * everyone. The swatches are at least 3:1 against the page and its fills in both themes
 * (iki.css), so a dot shows on any of them; its type's name is always beside it.
 */

import { swatchOf } from "./project-color";

/** Which swatch a type takes, from 1 to 8, from its name (`iki/dna.Plasmid`). */
export function typeSwatch(type: string): number {
  return swatchOf(type);
}

/** A type's dot, before its name; `large` beside a page's title. Decorative: the name is
 * there to read. `className` places it as its neighbours are; its color is its own. */
export function TypeDot({
  type,
  large = false,
  className,
}: {
  type: string;
  large?: boolean;
  className?: string;
}) {
  return (
    <span
      className={["type-dot", large ? "type-dot-large" : "", className ?? ""]
        .filter(Boolean)
        .join(" ")}
      data-swatch={typeSwatch(type)}
      aria-hidden="true"
    />
  );
}

/** A type's name with its dot before it, as a cell or a list shows it. */
export function TypeName({ type, label }: { type: string; label: string }) {
  return (
    <span className="type-name">
      <TypeDot type={type} />
      {label}
    </span>
  );
}
