/**
 * How much there is of a record (ADR 0139): its amount, whether a state holds it, the
 * controls that draw, transfer, split and adjust it, and its ledger.
 *
 * An adjustment is a count: it's checked against the balance the person saw when they
 * started it (If-Balance-Revision). If anything changed it since, nothing is recorded: the
 * dialog shows the balance now and what changed it, keeps what they entered, and they
 * recount before recording again.
 */

import { unwrap, type Schemas } from "@ikisci/sdk";
import {
  infiniteQueryOptions,
  useInfiniteQuery,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useId, useRef, useState } from "react";

import { fieldErrors, iki, problemOf, useApiMutation } from "../api";
import { ENTITIES, personName, type Entity, type EntityType, type FieldDef } from "../entities";
import { RecordPicker } from "../entity-inputs";
import type { Picked } from "../entity-values";
import {
  Button,
  ConfirmDialog,
  Loading,
  ProblemAlert,
  RelativeTime,
  SelectField,
  TextField,
  Time,
  useMessages,
} from "../ui";

type Movement = Schemas["MovementOut"];
type Amount = Schemas["AmountOut"];
/** The balance revision of a record with no amount recorded yet. */
const UNRECORDED = "unrecorded";
const DRAW_PURPOSES = [
  { value: "routine", label: "Routine use" },
  { value: "qa_sampling", label: "QA sampling" },
] as const;
type Doing = "draw" | "transfer" | "split" | "adjust";
interface Done {
  moved: Schemas["AmountMovedOut"];
  aliquots: Schemas["RefOut"][];
}

export function movementsQuery(ref: string) {
  return infiniteQueryOptions({
    queryKey: [...ENTITIES, "movements", ref.toUpperCase()],
    initialPageParam: undefined as string | undefined,
    queryFn: async ({ pageParam }) =>
      unwrap(
        await iki.GET("/api/v1/entities/{ref}/amount/movements", {
          params: {
            path: { ref },
            query: { limit: 25, ...(pageParam ? { cursor: pageParam } : {}) },
          },
        }),
      ),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
}

const KINDS: Record<Movement["kind"], string> = {
  recorded: "Recorded",
  drawn: "Drawn",
  transferred_out: "Transferred to",
  transferred_in: "Transferred from",
  aliquoted: "Split into aliquots",
  aliquot: "Split from",
  adjusted: "Adjusted",
};

/** An ISO time in microseconds, as exact as the server's revisions: a JavaScript Date
 * keeps only milliseconds, and two changes can fall within one. */
function micros(iso: string): number {
  const [, whole = iso, fraction = "", zone = ""] =
    /^(.*T\d\d:\d\d:\d\d)(?:\.(\d+))?(.*)$/.exec(iso) ?? [];
  return Date.parse(`${whole}${zone}`) * 1000 + Number(fraction.padEnd(6, "0").slice(0, 6));
}

/** What a movement changed, signed: "−20 uL", "+5 uL". */
function changeOf(m: Movement): string {
  return `${m.change.startsWith("-") ? `−${m.change.slice(1)}` : `+${m.change}`} ${m.unit}`;
}

/** The balance now, and the movements since the one a count was made against. */
function useBalanceSince(observed: Amount | null, ref: string, enabled: boolean) {
  const revision = observed?.updated_at ?? UNRECORDED;
  const observedValue = observed?.value;
  return useQuery({
    queryKey: ["balance-since", ref, revision],
    enabled,
    gcTime: 0,
    queryFn: async ({ signal }) => {
      const [now, ledger] = await Promise.all([
        iki.GET("/api/v1/entities/{ref}/amount", { params: { path: { ref } }, signal }),
        iki.GET("/api/v1/entities/{ref}/amount/movements", {
          params: { path: { ref }, query: { limit: 25 } },
          signal,
        }),
      ]);
      const moves = unwrap(ledger);
      let since = moves.items;
      if (revision !== UNRECORDED) {
        // A change sets the balance, then writes its movement: the movement that made the
        // balance counted from is the first one after its revision. It's dropped when its
        // balance says so: what remains changed it since.
        const seen = micros(revision);
        since = since.filter((m) => micros(m.created_at) > seen);
        const made = since.at(-1);
        if (made && made.balance === observedValue) since = since.slice(0, -1);
      }
      return {
        amount: now.response.status === 404 ? null : unwrap(now),
        since,
        refs: moves.refs as Record<string, Schemas["RefOut"]>,
      };
    },
  });
}

/** A record's amount: shown with its controls, for a type that keeps one. */
export function EntityAmount({
  entity,
  type,
  types,
}: {
  entity: Entity;
  type: EntityType | undefined;
  types: EntityType[];
}) {
  const id = useId();
  const [doing, setDoing] = useState<Doing | null>(null);
  const kept = type?.amount;
  if (!kept) return null;
  const amount = entity.amount;
  const label = kept.label;
  const holder = amount?.held_by ? type.fields.find((f) => f.name === amount.held_by) : undefined;
  const held = holder
    ? `Held: its ${holder.label.toLowerCase()} is ${
        holder.states.find((s) => s.name === entity.values[holder.name])?.label ??
        String(entity.values[holder.name])
      }, so none of it can be drawn, transferred or split off until that changes.`
    : null;
  const editing = entity.can.edit_amount && !entity.archived;
  const canDraw = !!entity.can.draw_purposes?.length;
  const routineRefusal = entity.draw_refusals?.["routine"];
  const taking = editing && amount != null && !held;
  const why = `${id}-held`;
  return (
    <section className="card" aria-labelledby={`${id}-title`}>
      <h2 className="section-title" id={`${id}-title`}>
        {label}
      </h2>
      <p className="amount">
        {amount ? (
          <>
            <span className="amount-value">
              {amount.value} {amount.unit}
            </span>
            {amount.display ? <span className="cell-meta">{amount.display}</span> : null}
          </>
        ) : (
          <span className="muted">None recorded.</span>
        )}
      </p>
      {routineRefusal ? (
        <div className="alert alert-warning" role="note">
          <p>{routineRefusal}</p>
        </div>
      ) : null}
      {held ? (
        <p className="muted" id={why}>
          {held}
        </p>
      ) : null}
      {editing && !canDraw ? (
        <p className="muted" id={`${id}-no-purpose`}>
          You don’t have permission to draw from this record.
        </p>
      ) : null}
      {editing ? (
        <div className="actions">
          {(["draw", "transfer", "split"] as const).map((what) => (
            <Button
              key={what}
              disabled={!taking || (what === "draw" && !canDraw)}
              describedBy={
                held ? why : what === "draw" && !canDraw ? `${id}-no-purpose` : undefined
              }
              onClick={() => {
                setDoing(what);
              }}
            >
              {what === "draw" ? "Draw" : what === "transfer" ? "Transfer" : "Split into aliquots"}
            </Button>
          ))}
          <Button
            variant="quiet"
            onClick={() => {
              setDoing("adjust");
            }}
          >
            {amount ? "Adjust" : `Record ${label.toLowerCase()}`}
          </Button>
        </div>
      ) : null}
      {amount ? <Ledger entity={entity} label={label} /> : null}
      {doing ? (
        <AmountDialog
          entity={entity}
          kept={kept}
          types={types}
          confirmDraw={!!type.storage?.confirm.includes("draw")}
          aliquotType={types.find((t) => t.name === (kept.aliquots ?? type.name))}
          doing={doing}
          onClose={() => {
            setDoing(null);
          }}
        />
      ) : null}
    </section>
  );
}

/** The required, unique fields an aliquot needs a value of its own for: one each. */
function ownFields(aliquotType: EntityType | undefined, kept: Schemas["TypeAmountOut"]) {
  return (aliquotType?.fields ?? []).filter(
    (f: FieldDef) =>
      f.required && f.unique && f.type === "string" && f.name !== kept.aliquots_of && !f.computed,
  );
}

function AmountDialog({
  entity,
  kept,
  types,
  confirmDraw,
  aliquotType,
  doing,
  onClose,
}: {
  entity: Entity;
  kept: Schemas["TypeAmountOut"];
  types: EntityType[];
  /** Its type asks for its label to be scanned before it's drawn from. */
  confirmDraw: boolean;
  aliquotType: EntityType | undefined;
  doing: Doing;
  onClose: () => void;
}) {
  const [value, setValue] = useState("");
  const [unit, setUnit] = useState(kept.unit);
  const [to, setTo] = useState("");
  const [count, setCount] = useState("2");
  const [reason, setReason] = useState("");
  const [usedIn, setUsedIn] = useState<Picked | null>(null);
  const [scanned, setScanned] = useState("");
  const purposes = DRAW_PURPOSES.filter((p) => entity.can.draw_purposes?.includes(p.value));
  const [purpose, setPurpose] = useState<Schemas["DrawPurpose"]>(purposes[0]?.value ?? "routine");
  const purposeAllowed = purposes.some((p) => p.value === purpose);
  const refusal = doing === "draw" && purposeAllowed ? entity.draw_refusals?.[purpose] : undefined;
  const own = ownFields(aliquotType, kept);
  const [names, setNames] = useState<Record<string, string>>({});
  // The balance a count is made against: as the dialog opened, or as last recounted. A
  // refetch behind the dialog doesn't change it.
  const [observed, setObserved] = useState<Amount | null>(entity.amount ?? null);
  const revision = observed?.updated_at ?? UNRECORDED;
  const countRef = useRef<HTMLDivElement>(null);
  const { say } = useMessages();
  const client = useQueryClient();
  const ref = entity.id;
  const quantity = { value: value.trim(), unit: unit.trim() };
  const lines = (name: string) =>
    (names[name] ?? "")
      .split("\n")
      .map((l) => l.trim())
      .filter(Boolean);
  const move = useApiMutation(async (): Promise<Done> => {
    switch (doing) {
      case "draw":
        return {
          moved: unwrap(
            await iki.POST("/api/v1/entities/{ref}/amount/draw", {
              params: { path: { ref } },
              body: {
                amount: quantity,
                purpose,
                reason: reason.trim(),
                used_in: usedIn?.id ?? null,
                confirm: scanned.trim(),
              },
            }),
          ),
          aliquots: [],
        };
      case "transfer":
        return {
          moved: unwrap(
            await iki.POST("/api/v1/entities/{ref}/amount/transfer", {
              params: { path: { ref } },
              body: { to: to.trim(), amount: quantity, reason: reason.trim() },
            }),
          ),
          aliquots: [],
        };
      case "adjust":
        return {
          moved: unwrap(
            await iki.POST("/api/v1/entities/{ref}/amount/adjust", {
              params: { path: { ref }, header: { "If-Balance-Revision": revision } },
              body: { amount: quantity, reason: reason.trim() },
            }),
          ),
          aliquots: [],
        };
      case "split": {
        const n = Number(count);
        const values = own.length
          ? Array.from({ length: n }, (_, i) =>
              Object.fromEntries(own.map((f) => [f.name, lines(f.name)[i] ?? ""])),
            )
          : null;
        const split = unwrap(
          await iki.POST("/api/v1/entities/{ref}/aliquots", {
            params: { path: { ref } },
            body: { count: n, each: quantity, values, reason: reason.trim() },
          }),
        );
        return { moved: split, aliquots: split.aliquots };
      }
    }
  }, [ENTITIES]);
  const problem = move.error ? problemOf(move.error) : undefined;
  const stale = doing === "adjust" && problem?.code === "stale_balance";
  const latest = useBalanceSince(observed, ref, stale);
  const errors = fieldErrors(problem);
  const amountError = errors["amount"] ?? errors["each"];
  const shownProblem =
    problem &&
    !stale &&
    !amountError &&
    !errors["purpose"] &&
    !errors["reason"] &&
    !errors["to"] &&
    !errors["count"] &&
    !errors["used_in"] &&
    !errors["confirm"]
      ? problem
      : undefined;
  const titles: Record<Doing, string> = {
    draw: `Draw from ${entity.hrid}`,
    transfer: `Transfer from ${entity.hrid}`,
    split: `Split ${entity.hrid} into aliquots`,
    adjust: observed
      ? `Adjust ${entity.hrid}’s ${kept.label.toLowerCase()}`
      : `Record ${entity.hrid}’s ${kept.label.toLowerCase()}`,
  };
  const confirm: Record<Doing, string> = {
    draw: "Draw",
    transfer: "Transfer",
    split: "Split",
    adjust: observed ? "Adjust" : "Record",
  };
  const recount = () => {
    if (!latest.data) return;
    setObserved(latest.data.amount);
    move.reset();
    // What they entered stays, selected, to keep or count again against the new balance.
    const field = countRef.current?.querySelector("input");
    field?.focus();
    field?.select();
  };
  return (
    <ConfirmDialog
      isOpen
      title={titles[doing]}
      confirmLabel={confirm[doing]}
      confirmVariant="primary"
      confirmDisabled={
        stale ||
        (doing === "draw" && (!purposeAllowed || !!refusal || (confirmDraw && !scanned.trim())))
      }
      busy={move.isPending}
      problem={shownProblem}
      onCancel={onClose}
      onConfirm={() => {
        move.mutate(undefined, {
          onError: (error) => {
            if (doing === "draw" && error.status === 403) {
              void client.invalidateQueries({ queryKey: [...ENTITIES, "one"] });
            }
            // The page behind shows the balance now, as the dialog does.
            if (error.problem.code === "stale_balance") {
              void client.invalidateQueries({ queryKey: ENTITIES });
            }
          },
          onSuccess: ({ moved, aliquots }) => {
            onClose();
            const left = `${moved.amount.value} ${moved.amount.unit}`;
            say(
              aliquots.length
                ? `Split ${entity.hrid} into ${aliquots.map((a) => a.hrid).join(", ")}: ${left} left.`
                : `${entity.hrid} has ${left} now.`,
            );
            void client.invalidateQueries({ queryKey: ENTITIES });
          },
        });
      }}
    >
      {stale ? (
        <StaleBalance
          observed={observed}
          latest={latest}
          onRecount={recount}
          what={kept.label.toLowerCase()}
        />
      ) : null}
      {doing === "adjust" ? (
        <p>
          {observed
            ? `It had ${observed.value} ${observed.unit} when this count began. If that changes before you record your count, you’ll recount it.`
            : "None is recorded yet. If someone records it before you do, you’ll recount it."}{" "}
          Each change is kept in its ledger, with your name and reason.
        </p>
      ) : entity.amount ? (
        <p>
          It has {entity.amount.value} {entity.amount.unit}. Each change is kept in its ledger, with
          your name and reason.
        </p>
      ) : null}
      {doing === "draw" ? (
        <SelectField
          label="Purpose"
          options={
            purposeAllowed
              ? purposes
              : [
                  {
                    value: "",
                    label: purposes.length ? "Choose a purpose" : "No draw purposes available",
                  },
                  ...purposes,
                ]
          }
          value={purposeAllowed ? purpose : ""}
          onChange={(selected) => {
            if (selected) setPurpose(selected);
            if (move.isError) move.reset();
          }}
          error={errors["purpose"]}
          {...(refusal ? { help: refusal } : {})}
        />
      ) : null}
      {doing === "draw" ? (
        <RecordPicker
          name="used_in"
          label="Used in"
          types={types}
          value={usedIn}
          onChange={setUsedIn}
          error={errors["used_in"]}
          help={`The experiment or record it went into: it’s linked to ${entity.hrid} in both’s lineage.`}
        />
      ) : null}
      {doing === "draw" && confirmDraw ? (
        <TextField
          label="Scan its label"
          value={scanned}
          onChange={(v) => {
            setScanned(v);
            if (move.isError) move.reset();
          }}
          maxLength={200}
          error={errors["confirm"]}
          help={`Scan ${entity.hrid}’s own label, to confirm it’s the one in your hand.`}
        />
      ) : null}
      {doing === "transfer" ? (
        <TextField
          label="To"
          value={to}
          onChange={setTo}
          maxLength={64}
          error={errors["to"]}
          help="The record it goes to, by its ID, such as SMP-000012."
        />
      ) : null}
      {doing === "split" ? (
        <TextField
          label="How many aliquots"
          value={count}
          onChange={setCount}
          maxLength={3}
          error={errors["count"]}
        />
      ) : null}
      <div className="amount-unit" ref={countRef}>
        <TextField
          label={doing === "split" ? "Each holds" : doing === "adjust" ? "It has" : "How much"}
          value={value}
          onChange={setValue}
          maxLength={40}
          error={amountError}
          {...(kept.concentration && doing !== "adjust"
            ? { help: "A volume, or a mass at its concentration." }
            : {})}
        />
        <TextField label="Unit" value={unit} onChange={setUnit} maxLength={50} />
      </div>
      {doing === "split"
        ? own.map((f) => (
            <TextField
              key={f.name}
              label={`Each aliquot’s ${f.label.toLowerCase()}`}
              value={names[f.name] ?? ""}
              onChange={(v) => {
                setNames({ ...names, [f.name]: v });
              }}
              multiline
              error={Object.entries(errors).find(([k]) => k.endsWith(`/${f.name}`))?.[1]}
              help="One a line, in order: each must be its own."
            />
          ))
        : null}
      <TextField
        label="Reason"
        value={reason}
        onChange={setReason}
        maxLength={2000}
        optional={doing !== "adjust"}
        multiline
        error={errors["reason"]}
        help={
          doing === "adjust"
            ? "Why it changed: a recount, a spill, a correction."
            : "What it was for. It’s kept in its ledger."
        }
      />
    </ConfirmDialog>
  );
}

/** A count refused because the balance changed after it was started: the balance now,
 * what changed it, and the way to recount. */
function StaleBalance({
  observed,
  latest,
  onRecount,
  what,
}: {
  observed: Amount | null;
  latest: ReturnType<typeof useBalanceSince>;
  onRecount: () => void;
  what: string;
}) {
  const now = latest.data?.amount;
  const since = latest.data?.since ?? [];
  const refs = latest.data?.refs ?? {};
  return (
    <div className="alert alert-warning" role="alert">
      <p>
        <strong>Your count wasn’t recorded:</strong> the {what} changed after you started counting.
      </p>
      {latest.isPending ? <Loading label="Reading the balance now…" /> : null}
      {latest.error ? <ProblemAlert problem={problemOf(latest.error)} /> : null}
      {latest.data ? (
        <>
          <p>
            {now ? (
              <>
                It has {now.value} {now.unit} now
                {observed ? (
                  <>
                    , not the {observed.value} {observed.unit} you started from
                  </>
                ) : null}
                .
              </>
            ) : (
              "None is recorded now."
            )}
          </p>
          {since.length ? (
            <ul aria-label="Changed since you started">
              {since.map((m) => {
                const other = m.counterpart ? refs[m.counterpart] : undefined;
                return (
                  <li key={m.id}>
                    {KINDS[m.kind]}
                    {other ? ` ${other.hrid}` : ""}: {changeOf(m)}, by {personName(m.user)},{" "}
                    <RelativeTime value={m.created_at} />
                    {m.reason ? ` (“${m.reason}”)` : ""}.
                  </li>
                );
              })}
            </ul>
          ) : null}
          <p>Recount it against this balance. What you entered is kept.</p>
        </>
      ) : null}
      <div className="actions">
        <Button
          busy={latest.isPending}
          busyText="Reading…"
          onClick={latest.error ? () => void latest.refetch() : onRecount}
        >
          {latest.error ? "Try again" : "Recount"}
        </Button>
      </div>
    </div>
  );
}

function Ledger({ entity, label }: { entity: Entity; label: string }) {
  const query = useInfiniteQuery(movementsQuery(entity.id));
  const pages = query.data?.pages ?? [];
  const items = pages.flatMap((p) => p.items);
  const refs = Object.assign({}, ...pages.map((p) => p.refs)) as Record<string, Schemas["RefOut"]>;
  return (
    <details className="disclosure">
      <summary>Its ledger</summary>
      {query.isPending ? <Loading label="Loading its ledger…" /> : null}
      {query.error ? <ProblemAlert problem={problemOf(query.error)} /> : null}
      {items.length ? (
        <div className="table-scroll">
          <table className="table">
            <caption className="visually-hidden">
              Changes to {entity.hrid}’s {label.toLowerCase()}, newest first
            </caption>
            <thead>
              <tr>
                <th scope="col">When</th>
                <th scope="col">What</th>
                <th scope="col">Change</th>
                <th scope="col">After</th>
                <th scope="col">By</th>
                <th scope="col">Why</th>
              </tr>
            </thead>
            <tbody>
              {items.map((m) => {
                const other = m.counterpart ? refs[m.counterpart] : undefined;
                return (
                  <tr key={m.id}>
                    <td>
                      <Time value={m.created_at} />
                    </td>
                    <td>
                      {KINDS[m.kind]}
                      {m.purpose ? (
                        <span className="cell-meta">
                          {DRAW_PURPOSES.find((p) => p.value === m.purpose)?.label}
                        </span>
                      ) : null}
                      {other ? (
                        <>
                          {" "}
                          <Link to="/entities/$ref/" params={{ ref: other.hrid }}>
                            {other.hrid}
                          </Link>
                        </>
                      ) : m.counterpart ? (
                        " a record you can’t read"
                      ) : null}
                    </td>
                    <td className="numeric">
                      {changeOf(m)}
                      {m.given && m.given.unit !== m.unit ? (
                        <span className="cell-meta">
                          {m.given.value} {m.given.unit}
                        </span>
                      ) : null}
                    </td>
                    <td className="numeric">
                      {m.balance} {m.unit}
                    </td>
                    <td>{personName(m.user)}</td>
                    <td>{m.reason}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : null}
      {query.hasNextPage ? (
        <div className="actions">
          <Button
            busy={query.isFetchingNextPage}
            busyText="Loading…"
            onClick={() => void query.fetchNextPage()}
          >
            Show older
          </Button>
        </div>
      ) : null}
    </details>
  );
}
