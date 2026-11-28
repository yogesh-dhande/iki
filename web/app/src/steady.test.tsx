/**
 * What holds still under a press (steady.ts), and the feedback that goes through it: a
 * field's error, a form's alert, and the confirmations at the foot of the page.
 */

import { ApiError, type Problem } from "@ikisci/sdk";
import {
  createMemoryHistory,
  createRootRoute,
  createRouter,
  RouterProvider,
} from "@tanstack/react-router";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState, type ReactNode } from "react";
import { describe, expect, it } from "vitest";

import { required, useForm } from "./forms";
import { useSteady, useSteadyBlock } from "./steady";
import { Button, MessagesProvider, ProblemAlert, TextField, useMessages } from "./ui";

/** Lets go of the press, and waits for the task after, where what was held is drawn. */
async function letGo(target: Window | Element = window) {
  fireEvent.pointerUp(target);
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve));
  });
}

function Shown({ value, waiting = false }: { value: string; waiting?: boolean }) {
  return <p>{useSteady(value, waiting)}</p>;
}

describe("useSteady", () => {
  it("holds a value as it was while a press is on, and draws it once it's let go", async () => {
    const { rerender } = render(<Shown value="3 members" />);
    fireEvent.pointerDown(screen.getByText("3 members"));
    rerender(<Shown value="4 members" />);
    expect(screen.getByText("3 members")).toBeVisible();
    // Let go: the click it makes is sent first, so it's drawn in the task after.
    fireEvent.pointerUp(window);
    expect(screen.getByText("3 members")).toBeVisible();
    await letGo();
    expect(screen.getByText("4 members")).toBeVisible();
  });

  it("holds while waiting, and a press that's cancelled lets go too", async () => {
    const { rerender } = render(<Shown value="Saved." />);
    rerender(<Shown value="" waiting />);
    expect(screen.getByText("Saved.")).toBeVisible();
    rerender(<Shown value="Check the highlighted fields." />);
    expect(screen.getByText("Check the highlighted fields.")).toBeVisible();

    fireEvent.pointerDown(document.body);
    rerender(<Shown value="Saved." />);
    expect(screen.getByText("Check the highlighted fields.")).toBeVisible();
    fireEvent.pointerCancel(window);
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve));
    });
    expect(screen.getByText("Saved.")).toBeVisible();
  });
});

function Block({ count }: { count: number }) {
  const [shown, onPointerDown] = useSteadyBlock(<p>{`${String(count)} still to check`}</p>);
  return (
    <>
      <section aria-label="Held" onPointerDown={onPointerDown}>
        {shown}
      </section>
      <button type="button">Elsewhere</button>
    </>
  );
}

describe("useSteadyBlock", () => {
  it("draws a block as it was while a press on it is on, and as it comes once it's let go", async () => {
    const { rerender } = render(<Block count={3} />);
    fireEvent.pointerDown(screen.getByText("3 still to check"));
    rerender(<Block count={2} />);
    expect(screen.getByText("3 still to check")).toBeVisible();
    // Let go: the click it makes lands first, on the block as it was.
    fireEvent.pointerUp(window);
    expect(screen.getByText("3 still to check")).toBeVisible();
    await letGo();
    expect(screen.getByText("2 still to check")).toBeVisible();
  });

  it("draws it as it comes while a press elsewhere is on", async () => {
    const { rerender } = render(<Block count={3} />);
    fireEvent.pointerDown(screen.getByRole("button", { name: "Elsewhere" }));
    rerender(<Block count={2} />);
    expect(screen.getByText("2 still to check")).toBeVisible();
    await letGo();
    // A press on it that the window loses is let go too.
    fireEvent.pointerDown(screen.getByText("2 still to check"));
    rerender(<Block count={1} />);
    expect(screen.getByText("2 still to check")).toBeVisible();
    fireEvent.blur(window);
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve));
    });
    expect(screen.getByText("1 still to check")).toBeVisible();
  });
});

function Reason({ initial }: { initial?: string }) {
  const [value, setValue] = useState("");
  const [error, setError] = useState(initial);
  return (
    <>
      <TextField
        label="Why it may stand"
        value={value}
        onChange={(next) => {
          setValue(next);
          setError(undefined);
        }}
        error={error}
      />
      <button
        type="button"
        onClick={() => {
          setError(value ? undefined : "Say why it may stand as it is.");
        }}
      >
        Clear the hold
      </button>
    </>
  );
}

describe("a field's error", () => {
  it("keeps its room while it's put right in its field, and gives it up once they leave", async () => {
    const user = userEvent.setup();
    render(<Reason initial="Say why it may stand as it is." />);
    const field = screen.getByRole("textbox", { name: "Why it may stand" });
    expect(field).toHaveAccessibleDescription("Say why it may stand as it is.");
    await user.click(field);
    await user.type(field, "Within its range on a retest");
    // Put right: it's gone, for people and screen readers alike, but its room stays.
    expect(screen.queryByText("Say why it may stand as it is.")).toBeNull();
    expect(field).not.toHaveAccessibleDescription();
    const room = document.querySelector(".field-error-room");
    expect(room).toHaveAttribute("aria-hidden", "true");
    expect(room?.textContent).toBe("");
    // Leaving by a press: the room stays till the press's click is in.
    const clear = screen.getByRole("button", { name: "Clear the hold" });
    fireEvent.pointerDown(clear);
    act(() => {
      clear.focus();
    });
    expect(document.querySelector(".field-error-room")).not.toBeNull();
    await letGo(clear);
    expect(document.querySelector(".field-error-room")).toBeNull();
  });

  it("doesn't come or go while a press is on", async () => {
    render(<Reason />);
    const clear = screen.getByRole("button", { name: "Clear the hold" });
    fireEvent.pointerDown(clear);
    fireEvent.click(clear);
    expect(screen.queryByText("Say why it may stand as it is.")).toBeNull();
    await letGo(clear);
    expect(screen.getByText("Say why it may stand as it is.")).toBeVisible();
  });

  it("is given no room in a field that's left, or never had one", async () => {
    const user = userEvent.setup();
    render(<Reason />);
    await user.click(screen.getByRole("textbox", { name: "Why it may stand" }));
    await user.keyboard("a");
    expect(document.querySelector(".field-error-room")).toBeNull();
  });
});

const PROBLEM: Problem = {
  type: "about:blank",
  title: "Conflict",
  status: 409,
  code: "conflict",
  detail: "A project is called Fragment Screen already.",
};

describe("a form's alert", () => {
  it("says what it said while it's sent again, till the answer comes", () => {
    const { rerender } = render(<ProblemAlert problem={PROBLEM} />);
    expect(screen.getByRole("alert")).toHaveTextContent(PROBLEM.detail);
    // Sent again: the error goes from the mutation, but the alert stays till the answer.
    rerender(<ProblemAlert problem={undefined} pending />);
    expect(screen.getByRole("alert")).toHaveTextContent(PROBLEM.detail);
    rerender(<ProblemAlert problem={undefined} />);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("points to the fields when the problem is about them", () => {
    render(
      <ProblemAlert problem={{ ...PROBLEM, errors: [{ pointer: "#/name", detail: "Taken" }] }} />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("Check the highlighted fields.");
  });
});

/** A form like New project's, whose sending the test answers. */
function NewProject({ sent }: { sent: (answer: (error: unknown) => void) => void }) {
  const [name, setName] = useState("");
  const [state, setState] = useState<{ pending: boolean; error: unknown }>({
    pending: false,
    error: null,
  });
  const form = useForm(state.error, state.pending);
  return (
    <>
      {form.alert}
      <form
        {...form.props}
        onSubmit={(event) => {
          event.preventDefault();
          if (!form.validate({ name: required(name, "Enter a name for the project.") })) return;
          setState({ pending: true, error: null });
          sent((error) => {
            setState({ pending: false, error });
          });
        }}
      >
        <TextField
          label="Project name"
          value={name}
          onChange={setName}
          error={form.errors["name"]}
        />
        <button type="submit">Create project</button>
      </form>
    </>
  );
}

describe("useForm", () => {
  it("keeps its alert and errors while it's sent again, then says what the answer says", async () => {
    const user = userEvent.setup();
    const answers: ((error: unknown) => void)[] = [];
    render(<NewProject sent={(answer) => answers.push(answer)} />);
    const create = screen.getByRole("button", { name: "Create project" });
    await user.click(create);
    const name = screen.getByRole("textbox", { name: "Project name" });
    expect(screen.getByRole("alert")).toHaveTextContent("Check the highlighted fields.");
    expect(name).toHaveFocus();

    await user.type(name, "Fragment Screen");
    await user.click(create);
    expect(answers).toHaveLength(1);
    // Being sent: nothing above Create project goes, and focus stays where it was pressed.
    expect(screen.getByRole("alert")).toHaveTextContent("Check the highlighted fields.");
    expect(screen.getByText("Enter a name for the project.")).toBeVisible();
    expect(create).toHaveFocus();

    act(() => {
      answers[0]?.(
        new ApiError({
          ...PROBLEM,
          status: 422,
          code: "validation_failed",
          errors: [{ pointer: "#/name", detail: "A project is called that already." }],
        }),
      );
    });
    // The answer's: said once, with focus on the field it's about.
    expect(await screen.findByText("A project is called that already.")).toBeVisible();
    expect(screen.queryByText("Enter a name for the project.")).toBeNull();
    expect(screen.getByRole("alert")).toHaveTextContent("Check the highlighted fields.");
    await waitFor(() => {
      expect(name).toHaveFocus();
    });
  });
});

function Saying({ children }: { children?: ReactNode }) {
  const { say } = useMessages();
  return (
    <>
      <h1>Kinase Atlas</h1>
      <Button
        onClick={() => {
          say("Saved.");
        }}
      >
        Save
      </Button>
      {children}
    </>
  );
}

async function renderWithMessages() {
  const root = createRootRoute({
    component: () => (
      <MessagesProvider>
        <Saying />
      </MessagesProvider>
    ),
  });
  const router = createRouter({
    routeTree: root,
    history: createMemoryHistory({ initialEntries: ["/"] }),
  });
  render(<RouterProvider router={router} />);
  await screen.findByRole("heading", { name: "Kinase Atlas" });
}

describe("confirmations", () => {
  it("come at the foot of the page, after what's on it, and can be dismissed", async () => {
    const user = userEvent.setup();
    await renderWithMessages();
    const save = screen.getByRole("button", { name: "Save" });
    await user.click(save);
    const status = screen.getByRole("status");
    expect(status).toHaveTextContent("Saved.");
    // After the page's controls in the document: nothing on the page is below it to move.
    expect(save.compareDocumentPosition(status) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(status.closest(".notices")).not.toBeNull();
    await user.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(screen.getByRole("status")).toBeEmptyDOMElement();
  });

  it("said while a press is on, come once it's let go", async () => {
    await renderWithMessages();
    const save = screen.getByRole("button", { name: "Save" });
    fireEvent.pointerDown(save);
    fireEvent.click(save);
    expect(screen.getByRole("status")).toBeEmptyDOMElement();
    await letGo(save);
    expect(screen.getByRole("status")).toHaveTextContent("Saved.");
  });
});
