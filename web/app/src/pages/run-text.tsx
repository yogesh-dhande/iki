/**
 * How the runs' pages say a run: its status, as a badge; and for a workflow's step, what must
 * hold for it to run.
 */

import {
  ATTEMPT_STATE,
  STATUS,
  tone,
  type Attempt,
  type RunStatus,
  type WorkflowRun,
} from "../runs";

/** A run's status, or one of its attempts', as a badge. */
export function StatusBadge({ status }: { status: RunStatus | Attempt["state"] }) {
  const label =
    status in STATUS ? STATUS[status as RunStatus] : ATTEMPT_STATE[status as Attempt["state"]];
  return <span className={`badge badge-${tone(status)}`}>{label}</span>;
}

type Condition = WorkflowRun["steps"][number]["when"][number];

const COMPARED: Record<Condition["operator"], string> = {
  is: "is",
  is_not: "isn’t",
  in: "is one of",
  at_least: "is at least",
  at_most: "is at most",
  above: "is above",
  below: "is below",
};

/** `$steps.align.metrics.score` as `align.score`, `$params.min` as `min`. */
function named(ref: string): string {
  const parts = ref.split(".");
  return parts[0] === "$steps" ? `${parts[1] ?? ""}.${parts[3] ?? ""}` : (parts[1] ?? ref);
}

function Operand({ value }: { value: unknown }) {
  if (Array.isArray(value)) {
    return (
      <>
        {(value as unknown[]).map((v, i) => (
          <span key={i}>
            {i ? ", " : ""}
            <Operand value={v} />
          </span>
        ))}
      </>
    );
  }
  if (typeof value === "string" && value.startsWith("$params.")) {
    return <code>{named(value)}</code>;
  }
  const literal = typeof value === "string" && value.startsWith("$$") ? value.slice(1) : value;
  return <code>{JSON.stringify(literal)}</code>;
}

/** "Runs if align.score is at least min_score": what must hold for a step to run. */
export function When({ conditions }: { conditions: Condition[] }) {
  return (
    <span className="row-meta">
      {"Runs if "}
      {conditions.map((c, i) => (
        <span key={i}>
          {i ? " and " : ""}
          <code>{named(c.value)}</code> {COMPARED[c.operator]} <Operand value={c.operand} />
        </span>
      ))}
    </span>
  );
}
