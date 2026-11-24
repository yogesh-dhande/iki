// Code and tables that are wider than the page scroll sideways on their own, so a reader
// who uses a keyboard must be able to reach them to scroll them (WCAG 2.1.1; axe's
// scrollable-region-focusable). This puts every rendered <pre> and <table> in the tab
// order; the site's own components do the same for theirs.
import { defineHastPlugin, type HastPluginDefinition } from "satteri";

export function focusableScrollers(): HastPluginDefinition {
  return defineHastPlugin({
    name: "iki-focusable-scrollers",
    element: {
      filter: ["pre", "table"],
      visit(node, context) {
        context.setProperty(node, "tabIndex", 0);
      },
    },
  });
}
