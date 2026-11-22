/**
 * The filter chips on their own, against the timing of a real address bar: the address
 * takes a moment to follow what the chips apply.
 */

import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import { queryClient } from "./api";
import { PROTEIN } from "./entity-fixtures";
import { ASSIGNED, filterable, type Filter } from "./filters";
import { FilterBar } from "./pages/entity-filters";

function Bar({
  applied,
  onApply,
  across = false,
}: {
  applied: Filter[];
  onApply: (f: Filter[]) => void;
  /** Across projects, with no project to pick people from. */
  across?: boolean;
}) {
  const [, setRenders] = useState(0);
  // The grid page re-renders while a navigation is pending (its list refetching, say),
  // passing new arrays that still hold the old address's filters.
  return (
    <QueryClientProvider client={queryClient}>
      <button
        type="button"
        onClick={() => {
          setRenders((n) => n + 1);
        }}
      >
        Re-render
      </button>
      <FilterBar
        type={PROTEIN}
        types={[PROTEIN]}
        fields={filterable(PROTEIN, [ASSIGNED])}
        applied={[...applied]}
        projectId={across ? undefined : "p"}
        noun="proteins"
        onApply={onApply}
      />
    </QueryClientProvider>
  );
}

/** Adds a filter by a field, from the Filter menu: its editor opens. */
async function add(user: ReturnType<typeof userEvent.setup>, field: string) {
  await user.click(screen.getByRole("button", { name: "Filter" }));
  const menu = await screen.findByRole("menu", { name: "Filter" });
  await user.click(within(menu).getByRole("menuitem", { name: new RegExp(`^${field}`) }));
  return screen.findByRole("dialog");
}

describe("the filter chips", () => {
  it("adds a filter from the menu, and keeps it while the address catches up", async () => {
    const user = userEvent.setup();
    const onApply = vi.fn();
    render(<Bar applied={[]} onApply={onApply} />);
    const editor = await add(user, "Name");
    await user.selectOptions(within(editor).getByLabelText("Filter 1: condition"), "is");
    await user.type(within(editor).getByLabelText("Filter 1: value"), "ABL1");
    await waitFor(() => {
      expect(onApply).toHaveBeenLastCalledWith([{ field: "name", op: "eq", value: "ABL1" }]);
    });
    await user.click(within(editor).getByRole("button", { name: "Done" }));
    const chips = screen.getByRole("group", { name: "Filters" });
    expect(within(chips).getByRole("button", { name: "Filter 1: Name is ABL1" })).toBeVisible();

    await user.click(screen.getByRole("button", { name: "Re-render" }));
    expect(within(chips).getByRole("button", { name: "Filter 1: Name is ABL1" })).toBeVisible();
    // It opens again to change it.
    await user.click(within(chips).getByRole("button", { name: "Filter 1: Name is ABL1" }));
    expect(within(await screen.findByRole("dialog")).getByLabelText("Filter 1: value")).toHaveValue(
      "ABL1",
    );
  });

  it("removes a filter with its chip's button", async () => {
    const user = userEvent.setup();
    const onApply = vi.fn();
    const applied: Filter[] = [{ field: "name", op: "contains", value: "kinase" }];
    render(<Bar applied={applied} onApply={onApply} />);
    await user.click(screen.getByRole("button", { name: "Remove filter 1: Name contains kinase" }));
    await waitFor(() => {
      expect(onApply).toHaveBeenLastCalledWith([]);
    });
  });

  it("finds a sequence by a motif, which only a sequence offers", async () => {
    const user = userEvent.setup();
    const onApply = vi.fn();
    render(<Bar applied={[]} onApply={onApply} />);
    await user.click(screen.getByRole("button", { name: "Filter" }));
    const menu = await screen.findByRole("menu", { name: "Filter" });
    // The menu says what each field takes: a sequence's motif first.
    expect(within(menu).getByRole("menuitem", { name: /^Sequence/ })).toHaveTextContent(
      "has the motif",
    );
    expect(within(menu).getByRole("menuitem", { name: /^Name/ })).not.toHaveTextContent(
      "has the motif",
    );
    await user.click(within(menu).getByRole("menuitem", { name: /^Sequence/ }));
    const editor = await screen.findByRole("dialog");
    expect(within(editor).getByLabelText("Filter 1: condition")).toHaveDisplayValue(
      "has the motif",
    );
    // N, any residue, then S: an N-glycosylation sequon (N-X-S/T), as IUPAC codes write it.
    await user.type(within(editor).getByLabelText("Filter 1: value"), "NXS");
    await waitFor(() => {
      expect(onApply).toHaveBeenLastCalledWith([{ field: "sequence", op: "motif", value: "NXS" }]);
    });
    // The editor is a dialog, which hides the page behind it until it's done.
    await user.click(within(editor).getByRole("button", { name: "Done" }));
    expect(
      screen.getByRole("button", { name: "Filter 1: Sequence has the motif NXS" }),
    ).toBeVisible();
  });

  it("filters by how entities were made, all of one making", async () => {
    const user = userEvent.setup();
    const onApply = vi.fn();
    render(<Bar applied={[]} onApply={onApply} />);
    const editor = await add(user, "How it was made");
    await user.type(within(editor).getByLabelText("Filter 1: software"), "AlphaFold");
    await waitFor(() => {
      expect(onApply).toHaveBeenLastCalledWith([
        { field: "made", op: "has", value: { software: "AlphaFold" } },
      ]);
    });
    await user.selectOptions(within(editor).getByLabelText("Filter 1: how"), "computed");
    await user.selectOptions(
      within(editor).getByLabelText("Filter 1: who says so"),
      "as Iki recorded",
    );
    await waitFor(() => {
      expect(onApply).toHaveBeenLastCalledWith([
        {
          field: "made",
          op: "has",
          value: { software: "AlphaFold", kind: "computational", by: "iki" },
        },
      ]);
    });
    await user.click(within(editor).getByRole("button", { name: "Done" }));
    const chip = screen.getByRole("button", {
      name: "Filter 1: How it was made: AlphaFold, computed, as Iki recorded",
    });
    await user.click(chip);
    const again = await screen.findByRole("dialog");
    await user.selectOptions(within(again).getByLabelText("Filter 1: condition"), "isn’t recorded");
    await waitFor(() => {
      expect(onApply).toHaveBeenLastCalledWith([{ field: "made", op: "is_set", value: false }]);
    });
  });

  it("shows lineage and makings from the address, in their words", async () => {
    const user = userEvent.setup();
    const applied: Filter[] = [
      { field: "upstream", op: "of", value: "RES-000004" },
      { field: "made", op: "has", value: { software: "Boltz", by: "source" } },
    ];
    render(<Bar applied={applied} onApply={vi.fn()} />);
    expect(
      screen.getByRole("button", { name: "Filter 1: Lineage was used to make RES-000004" }),
    ).toBeVisible();
    await user.click(
      screen.getByRole("button", {
        name: "Filter 2: How it was made: Boltz, as its source says",
      }),
    );
    const editor = await screen.findByRole("dialog");
    expect(within(editor).getByLabelText("Filter 2: software")).toHaveValue("Boltz");
    expect(within(editor).getByLabelText("Filter 2: how")).toHaveValue("");
    expect(within(editor).getByLabelText("Filter 2: who says so")).toHaveValue("source");
  });

  it("shows the address's filters when it changes by other means", () => {
    const onApply = vi.fn();
    const { rerender } = render(<Bar applied={[]} onApply={onApply} />);
    const contains: Filter = { field: "name", op: "contains", value: "src" };
    rerender(<Bar applied={[contains]} onApply={onApply} />);
    expect(screen.getByRole("button", { name: "Filter 1: Name contains src" })).toBeVisible();
    rerender(<Bar applied={[]} onApply={onApply} />);
    expect(screen.queryByRole("button", { name: /^Filter 1:/ })).not.toBeInTheDocument();
    expect(onApply).not.toHaveBeenCalled();
  });

  it("across projects, filters by people as me, set or empty: there's nobody to pick", async () => {
    const user = userEvent.setup();
    render(<Bar applied={[]} onApply={vi.fn()} across />);
    const editor = await add(user, "Assigned to");
    const options = within(within(editor).getByLabelText("Filter 1: condition"))
      .getAllByRole("option")
      .map((o) => o.textContent);
    expect(options).toEqual(["is me", "is set", "is empty"]);
  });
});
