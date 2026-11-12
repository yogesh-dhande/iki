/**
 * Budgets (ADR 0071): what the organization, a team or a project may spend on runs'
 * machines in a month. Compute Admins set them; Auditors and Org Admins see them.
 */

import { unwrap } from "@ikisci/sdk";
import { useState, type SubmitEvent } from "react";
import { Dialog, Heading, Modal, ModalOverlay } from "react-aria-components";

import { iki, problemOf, useApiMutation } from "../api";
import { monthName, parseWarnAt, warnAtText, type Budget } from "../costs";
import { useForm } from "../forms";
import { Button, CheckboxField, ConfirmDialog, TextField, useMessages } from "../ui";
import { BudgetMeter, EnforceBadge } from "./budget-meter";

/** Whose budget a new one is: the organization's, a team's or a project's. */
export interface BudgetScope {
  kind: "organization" | "team" | "project";
  id?: string;
  name: string;
}

export function scopeOf(budget: Budget): BudgetScope {
  if (budget.project) return { kind: "project", id: budget.project.id, name: budget.project.name };
  if (budget.team) return { kind: "team", id: budget.team.id, name: budget.team.name };
  return { kind: "organization", name: "The organization" };
}

const INVALIDATE = [["costs"]] as const;

/** Sets a new budget for `scope`, or changes `budget`. */
export function BudgetDialog({
  scope,
  budget,
  onClose,
}: {
  scope: BudgetScope;
  budget?: Budget;
  onClose: () => void;
}) {
  const { say } = useMessages();
  const [amount, setAmount] = useState(budget ? budget.amount.toFixed(2) : "");
  const [warnAt, setWarnAt] = useState((budget?.warn_at ?? [50, 80, 100]).join(", "));
  const [enforce, setEnforce] = useState(budget?.enforce ?? false);
  const save = useApiMutation(
    async (body: { amount: string; warn_at: number[]; enforce: boolean }) =>
      budget
        ? unwrap(
            await iki.PATCH("/api/v1/compute/budgets/{budget_id}", {
              params: { path: { budget_id: budget.id } },
              body,
            }),
          )
        : unwrap(
            await iki.POST("/api/v1/compute/budgets", {
              body: {
                ...body,
                ...(scope.kind === "team" && scope.id ? { team: scope.id } : {}),
                ...(scope.kind === "project" && scope.id ? { project: scope.id } : {}),
              },
            }),
          ),
    INVALIDATE,
  );
  const form = useForm(save.error);
  const whose = scope.kind === "organization" ? "the organization" : scope.name;

  function submit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    const levels = parseWarnAt(warnAt);
    const number = Number(amount);
    const ok = form.validate({
      amount:
        !amount.trim() || !Number.isFinite(number) || number <= 0
          ? "Enter what it may spend in a month, such as 500."
          : !/^\d+(\.\d{1,2})?$/.test(amount.trim()) &&
            "Give it to the cent at most, such as 499.99.",
      warn_at: !levels && "Give percentages from 1 to 100, such as 50, 80, 100.",
    });
    if (!ok || !levels) return;
    save.mutate(
      { amount: amount.trim(), warn_at: levels, enforce },
      {
        onSuccess: () => {
          say(budget ? `Changed the budget for ${whose}.` : `Set a budget for ${whose}.`);
          onClose();
        },
      },
    );
  }

  return (
    <ModalOverlay
      className="dialog-overlay"
      isOpen
      isDismissable={!save.isPending}
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <Modal className="dialog">
        <Dialog className="dialog-body">
          <Heading slot="title" className="section-title">
            {budget ? `Budget for ${whose}` : `Set a budget for ${whose}`}
          </Heading>
          <form {...form.props} onSubmit={submit}>
            {form.alert}
            <TextField
              label="A month’s budget"
              value={amount}
              onChange={setAmount}
              error={form.errors["amount"]}
              help="In the currency of this deployment’s prices. Months are calendar months, in UTC."
            />
            <TextField
              label="Warn at"
              value={warnAt}
              onChange={setWarnAt}
              error={form.errors["warn_at"]}
              help="Percentages of the budget. Compute Admins, and a project’s Admins for its budget, are told once a month as spending passes each."
            />
            <CheckboxField
              label="Stop new container and VM runs once it’s spent"
              checked={enforce}
              onChange={setEnforce}
              help="Runs already going finish. A launch whose runs would typically cost more than what’s left is refused."
            />
            <div className="actions">
              <Button variant="primary" type="submit" busy={save.isPending} busyText="Saving…">
                {budget ? "Save" : "Set budget"}
              </Button>
              <Button variant="quiet" onClick={onClose} disabled={save.isPending}>
                Cancel
              </Button>
            </div>
          </form>
        </Dialog>
      </Modal>
    </ModalOverlay>
  );
}

function RemoveBudget({ budget, onClose }: { budget: Budget; onClose: () => void }) {
  const { say } = useMessages();
  const scope = scopeOf(budget);
  const remove = useApiMutation(async () => {
    unwrap(
      await iki.DELETE("/api/v1/compute/budgets/{budget_id}", {
        params: { path: { budget_id: budget.id } },
      }),
    );
  }, INVALIDATE);
  return (
    <ConfirmDialog
      isOpen
      title={`Remove the budget for ${scope.kind === "organization" ? "the organization" : scope.name}?`}
      confirmLabel="Remove budget"
      busy={remove.isPending}
      problem={remove.error ? problemOf(remove.error) : undefined}
      onCancel={onClose}
      onConfirm={() => {
        remove.mutate(undefined, {
          onSuccess: () => {
            say("Removed the budget.");
            onClose();
          },
        });
      }}
    >
      <p>
        Its spending is no longer warned of
        {budget.enforce ? ", and runs it was holding back can start" : ""}. What was spent stays
        recorded.
      </p>
    </ConfirmDialog>
  );
}

/** Every budget, with this month's spending; Compute Admins change them here. */
export function Budgets({
  budgets,
  canManage,
  onSet,
}: {
  budgets: Budget[];
  canManage: boolean;
  onSet: (scope: BudgetScope) => void;
}) {
  const [editing, setEditing] = useState<Budget | undefined>();
  const [removing, setRemoving] = useState<Budget | undefined>();
  const month = budgets[0]?.month;
  const hasOrg = budgets.some((b) => !b.team && !b.project);
  return (
    <section className="card" aria-labelledby="budgets-title">
      <h2 className="section-title" id="budgets-title">
        Budgets{month ? ` for ${monthName(month)}` : ""}
      </h2>
      {budgets.length ? (
        <div className="table-scroll">
          <table className="table">
            <thead>
              <tr>
                <th scope="col">For</th>
                <th scope="col">Spent this month</th>
                <th scope="col">When it’s spent</th>
                {canManage ? (
                  <th scope="col">
                    <span className="visually-hidden">Change</span>
                  </th>
                ) : null}
              </tr>
            </thead>
            <tbody>
              {budgets.map((budget) => {
                const scope = scopeOf(budget);
                return (
                  <tr key={budget.id}>
                    <th scope="row">
                      {scope.name}
                      <span className="cell-meta">
                        {scope.kind === "team"
                          ? "Team · "
                          : scope.kind === "project"
                            ? "Project · "
                            : ""}
                        Warns at {warnAtText(budget.warn_at)}
                      </span>
                    </th>
                    <td>
                      <BudgetMeter status={budget} label={`Spent of ${scope.name}’s budget`} />
                    </td>
                    <td>
                      <EnforceBadge status={budget} />
                    </td>
                    {canManage ? (
                      <td className="row-actions">
                        <Button
                          variant="quiet"
                          label={`Change ${scope.name}’s budget`}
                          onClick={() => {
                            setEditing(budget);
                          }}
                        >
                          Change
                        </Button>
                        <Button
                          variant="quiet"
                          label={`Remove ${scope.name}’s budget`}
                          onClick={() => {
                            setRemoving(budget);
                          }}
                        >
                          Remove
                        </Button>
                      </td>
                    ) : null}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="muted">
          No budgets.{" "}
          {canManage
            ? "Set one for the organization, or for a team or project from its row above."
            : "Compute Admins set them."}
        </p>
      )}
      {canManage && !hasOrg ? (
        <p>
          <Button
            onClick={() => {
              onSet({ kind: "organization", name: "The organization" });
            }}
          >
            Set an organization budget
          </Button>
        </p>
      ) : null}
      {editing ? (
        <BudgetDialog
          scope={scopeOf(editing)}
          budget={editing}
          onClose={() => {
            setEditing(undefined);
          }}
        />
      ) : null}
      {removing ? (
        <RemoveBudget
          budget={removing}
          onClose={() => {
            setRemoving(undefined);
          }}
        />
      ) : null}
    </section>
  );
}
