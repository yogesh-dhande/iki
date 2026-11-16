/**
 * The filter panel on its own, against the timing of a real address bar: the
 * address takes a moment to follow what the panel applies.
 */

import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import { queryClient } from "./api";
import { PROTEIN } from "./entity-fixtures";
import { filterable, type Filter } from "./filters";
import { FilterPanel } from "./pages/entity-filters";

function Panel({ applied, onApply }: { applied: Filter[]; onApply: (f: Filter[]) => void }) {
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
      <FilterPanel
        id="filters"
        type={PROTEIN}
        types={[PROTEIN]}
        fields={filterable(PROTEIN)}
        applied={[...applied]}
        projectId="p"
        onApply={onApply}
      />
    </QueryClientProvider>
  );
}

describe("the filter panel", () => {
  it("keeps what's being edited while the address catches up", async () => {
    const user = userEvent.setup();
    const onApply = vi.fn();
    render(<Panel applied={[]} onApply={onApply} />);
    const panel = screen.getByRole("region", { name: "Filters" });
    await user.click(within(panel).getByRole("button", { name: "Add a filter" }));
    await user.selectOptions(within(panel).getByLabelText("Filter 1: field"), "Name");
    await user.selectOptions(within(panel).getByLabelText("Filter 1: condition"), "is");
    await user.type(within(panel).getByLabelText("Filter 1: value"), "ABL1");
    await waitFor(() => {
      expect(onApply).toHaveBeenLastCalledWith([{ field: "name", op: "eq", value: "ABL1" }]);
    });

    await user.click(screen.getByRole("button", { name: "Re-render" }));
    await user.click(within(panel).getByRole("button", { name: "Add a filter" }));
    expect(within(panel).getByLabelText("Filter 1: value")).toHaveValue("ABL1");
    expect(within(panel).getByLabelText("Filter 2: field")).toBeInTheDocument();
  });

  it("filters by how entities were made, all of one making", async () => {
    const user = userEvent.setup();
    const onApply = vi.fn();
    render(<Panel applied={[]} onApply={onApply} />);
    const panel = screen.getByRole("region", { name: "Filters" });
    await user.click(within(panel).getByRole("button", { name: "Add a filter" }));
    await user.selectOptions(within(panel).getByLabelText("Filter 1: field"), "How it was made");
    await user.type(within(panel).getByLabelText("Filter 1: software"), "AlphaFold");
    await waitFor(() => {
      expect(onApply).toHaveBeenLastCalledWith([
        { field: "made", op: "has", value: { software: "AlphaFold" } },
      ]);
    });
    await user.selectOptions(within(panel).getByLabelText("Filter 1: how"), "computed");
    await user.selectOptions(
      within(panel).getByLabelText("Filter 1: who says so"),
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
    await user.selectOptions(within(panel).getByLabelText("Filter 1: condition"), "isn’t recorded");
    await waitFor(() => {
      expect(onApply).toHaveBeenLastCalledWith([{ field: "made", op: "is_set", value: false }]);
    });
  });

  it("shows a making's filter from the address", () => {
    const applied: Filter[] = [
      { field: "made", op: "has", value: { software: "Boltz", by: "source" } },
    ];
    render(<Panel applied={applied} onApply={vi.fn()} />);
    const panel = screen.getByRole("region", { name: "Filters" });
    expect(within(panel).getByLabelText("Filter 1: software")).toHaveValue("Boltz");
    expect(within(panel).getByLabelText("Filter 1: how")).toHaveValue("");
    expect(within(panel).getByLabelText("Filter 1: who says so")).toHaveValue("source");
  });

  it("shows the address's filters when it changes by other means", () => {
    const onApply = vi.fn();
    const { rerender } = render(<Panel applied={[]} onApply={onApply} />);
    const contains: Filter = { field: "name", op: "contains", value: "src" };
    rerender(<Panel applied={[contains]} onApply={onApply} />);
    expect(screen.getByLabelText("Filter 1: value")).toHaveValue("src");
    rerender(<Panel applied={[]} onApply={onApply} />);
    expect(screen.queryByLabelText("Filter 1: value")).not.toBeInTheDocument();
    expect(onApply).not.toHaveBeenCalled();
  });
});
