import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { valueText } from "./entities";
import { field } from "./entity-fixtures";
import { keyLabel, objectList, objectListText } from "./object-lists";
import { ObjectTable } from "./pages/entity";

/** A plasmid's annotated features, as the plasmid plugin's annotation writes them: pUC19's
 * lac promoter and origin, each with the INSDC record its part's bases are from. */
const FEATURES = [
  {
    end: 591,
    name: "lac promoter",
    type: "promoter",
    start: 508,
    strand: "reverse",
    qualifiers: { label: "lac promoter", source: "INSDC LT009457.1 3307..3390" },
  },
  {
    end: 1455,
    name: "ori",
    type: "rep_origin",
    start: 867,
    strand: "reverse",
    qualifiers: { label: "ori (ColE1/pMB1/pBR322/pUC origin of replication)" },
  },
];

const ANNOTATED = field({ name: "annotated_features", label: "Annotated features", type: "json" });

describe("a list of objects", () => {
  it("is a table of its plain keys, name first, start and end as one position", () => {
    const list = objectList(FEATURES);
    expect(list?.columns.map((c) => c.label)).toEqual(["Name", "Type", "Position", "Strand"]);
    const position = list?.columns[2];
    expect(position?.numeric).toBe(true);
    expect(FEATURES.map((f) => position?.text(f))).toEqual(["508–591", "867–1,455"]);
    expect(list?.names).toEqual(["lac promoter", "ori"]);
  });

  it("reads in a grid's cell as how many and their names, never as JSON", () => {
    const text = valueText(ANNOTATED, FEATURES);
    expect(text).toBe("2 · lac promoter, ori");
    expect(text).not.toContain("{");
    const many = Array.from({ length: 7 }, (_, i) => ({ name: `part ${String(i + 1)}` }));
    expect(valueText(ANNOTATED, many)).toBe("7 · part 1, part 2, part 3, part 4, part 5, …");
    const unnamed = objectList([{ start: 1, end: 2 }]);
    expect(unnamed && objectListText(unnamed)).toBe("1 item");
  });

  it("isn't one when its items aren't all objects, or there are none", () => {
    expect(objectList([])).toBeNull();
    expect(objectList(["a", "b"])).toBeNull();
    expect(objectList([{ a: 1 }, [1, 2]])).toBeNull();
    expect(objectList({ name: "one" })).toBeNull();
    // Lists of strings read as they did.
    expect(valueText(field({ name: "tags", label: "Tags", type: "list" }), ["a", "b"])).toBe(
      "a, b",
    );
  });

  it("names its keys in words", () => {
    expect(keyLabel("rep_origin")).toBe("Rep origin");
    expect(keyLabel("copyNumber")).toBe("Copy number");
  });

  it("shows its first rows on a record's page, and the rest on asking", async () => {
    const parts = Array.from({ length: 12 }, (_, i) => ({
      name: `part ${String(i + 1)}`,
      start: i * 10 + 1,
      end: i * 10 + 9,
    }));
    const list = objectList(parts);
    if (!list) throw new Error("not a list of objects");
    render(<ObjectTable list={list} label="Annotated features" />);
    const table = screen.getByRole("table", { name: "Annotated features" });
    expect(
      within(table)
        .getAllByRole("columnheader")
        .map((h) => h.textContent),
    ).toEqual(["Name", "Position"]);
    // A header row and ten.
    expect(within(table).getAllByRole("row")).toHaveLength(11);
    expect(within(table).getByRole("row", { name: "part 1 1–9" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Show all 12" }));
    expect(within(table).getAllByRole("row")).toHaveLength(13);
    expect(screen.getByRole("button", { name: "Show fewer" })).toHaveAttribute(
      "aria-expanded",
      "true",
    );
  });
});
