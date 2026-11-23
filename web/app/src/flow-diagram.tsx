/**
 * A diagram of a state field's flow (ADR 0199): its states, who moves a record into each
 * and what they sign, where a record may come from, and where an edit returns it. Its
 * states are buttons, in order, each described by the same flow in words.
 *
 * It draws plain data, a flow (./flow.ts) and lab roles' labels, and says what's chosen
 * through `onSelect`: the shape the @ikisci/ui custom element is to have (ADR 0191), with
 * `onSelect` as its `iki-flow-select` event. Until that package is published, it lives
 * here.
 */

import { useQuery } from "@tanstack/react-query";
import { useEffect, useId, useMemo, useRef, useState, type KeyboardEvent } from "react";

import { labRolesQuery } from "./api";
import type { FlowField } from "./flow";
import {
  flowModel,
  layout,
  nextFrom,
  signatureName,
  whatItMeans,
  wayIn,
  whoSets,
  type FlowModel,
  type FlowNode,
} from "./flow-model";

/** What the person reading a record's page may do with its state field now: its
 * `can.states`. */
export interface FlowOption {
  name: string;
  allowed: boolean;
  why_not: string | null;
}

export interface FlowDiagramProps {
  flow: FlowField;
  /** Lab roles' labels, by role. */
  roles: Readonly<Record<string, string>>;
  /** The state a record is in, on its page. */
  current?: string | undefined;
  /** The states the reader may set it to now, and why not of the others. */
  options?: readonly FlowOption[] | undefined;
  /** The flow in words, below the diagram, or for screen readers only. */
  words?: "shown" | "hidden";
  /** A state chosen, or none. */
  onSelect?: (state: string | null) => void;
}

/** Lab roles' labels (ADR 0116), by role: what the API calls them, "QA (Lots)" where two
 * plugins' roles share a label. */
export function useRoleLabels(): Record<string, string> {
  const roles = useQuery(labRolesQuery).data;
  return useMemo(() => Object.fromEntries((roles ?? []).map((r) => [r.name, r.label])), [roles]);
}

export function FlowDiagram({
  flow,
  roles,
  current,
  options,
  words = "shown",
  onSelect,
}: FlowDiagramProps) {
  const id = useId();
  const model = useMemo(() => flowModel(flow, roles), [flow, roles]);
  const figure = useRef<HTMLElement>(null);
  const [width, setWidth] = useState(0);
  const [selected, setSelected] = useState<string | null>(null);
  const [pointed, setPointed] = useState<string | null>(null);
  const buttons = useRef(new Map<string, HTMLButtonElement>());

  useEffect(() => {
    const el = figure.current;
    if (!el) return;
    const measure = () => {
      setWidth(el.clientWidth);
    };
    measure();
    if (typeof ResizeObserver !== "function") return;
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => {
      observer.disconnect();
    };
  }, []);

  const placed = useMemo(() => layout(model, width), [model, width]);
  // States one above another leave room beside them on a wide page: the words go there.
  const beside =
    words === "shown" && placed.orientation === "column" && width - placed.width >= 440;
  const active = pointed ?? selected;
  const allowed = new Set((options ?? []).filter((o) => o.allowed).map((o) => o.name));
  const choose = (name: string | null) => {
    setSelected(name);
    onSelect?.(name);
  };
  const wordsId = (name: string) => `${id}-${name}`;

  const move = (event: KeyboardEvent, index: number) => {
    const last = model.nodes.length - 1;
    const to =
      event.key === "ArrowRight" || event.key === "ArrowDown"
        ? Math.min(last, index + 1)
        : event.key === "ArrowLeft" || event.key === "ArrowUp"
          ? Math.max(0, index - 1)
          : event.key === "Home"
            ? 0
            : event.key === "End"
              ? last
              : null;
    if (event.key === "Escape" && selected) {
      event.preventDefault();
      choose(null);
      return;
    }
    if (to === null) return;
    event.preventDefault();
    const name = model.nodes[to]?.name;
    if (name) buttons.current.get(name)?.focus();
  };

  const kinds = {
    move: placed.edges.some((e) => e.kind === "move"),
    correction: placed.edges.some((e) => e.kind === "correction"),
    any: placed.stubs.length > 0,
    start: model.nodes.some((n) => n.initial),
    locks: model.nodes.some((n) => n.locks),
    signed: model.nodes.some((n) => n.signatures.length),
  };
  const lit = (from: string, to: string) => active !== null && (from === active || to === active);

  return (
    <figure
      ref={figure}
      className={beside ? "flow is-beside" : "flow"}
      style={
        beside ? { gridTemplateColumns: `${String(placed.width)}px minmax(0, 1fr)` } : undefined
      }
      aria-labelledby={`${id}-caption`}
    >
      <figcaption className="visually-hidden" id={`${id}-caption`}>
        How {model.label} moves between its states
      </figcaption>
      <div className="flow-canvas">
        <div
          className={`flow-plane flow-${placed.orientation}`}
          style={{ width: placed.width, height: placed.height }}
        >
          <svg
            className="flow-lines"
            aria-hidden="true"
            width={placed.width}
            height={placed.height}
            viewBox={`0 0 ${String(placed.width)} ${String(placed.height)}`}
          >
            {placed.edges.map((e) => {
              const state = [
                `flow-edge-${e.kind}`,
                e.kind === "move" && current && e.from === current && allowed.has(e.to)
                  ? "is-next"
                  : "",
                lit(e.from, e.to) ? "is-lit" : active ? "is-dim" : "",
              ].join(" ");
              return (
                <g key={e.id} className={`flow-edge ${state}`} data-edge={e.id}>
                  <path className="flow-edge-line" d={e.path} />
                  <path className="flow-edge-head" d={e.head} />
                </g>
              );
            })}
            {placed.stubs.map((s) => {
              const state = [
                current && allowed.has(s.to) ? "is-next" : "",
                active === s.to ? "is-lit" : active ? "is-dim" : "",
              ].join(" ");
              return (
                <g
                  key={s.to}
                  className={`flow-edge flow-edge-any ${state}`}
                  data-edge={`*>${s.to}`}
                >
                  <circle className="flow-any-dot" cx={s.cx} cy={s.cy} r={3.5} />
                  <path className="flow-edge-line" d={s.path} />
                  <path className="flow-edge-head" d={s.head} />
                </g>
              );
            })}
          </svg>
          <ol className="flow-nodes" aria-label={`${model.label}’s states`}>
            {model.nodes.map((node, index) => {
              const box = placed.nodes[index];
              if (!box) return null;
              const classes = [
                "flow-node",
                node.initial ? "is-initial" : "",
                node.locks ? "is-locking" : "",
                node.name === current ? "is-current" : "",
                current && allowed.has(node.name) ? "is-next" : "",
                node.name === selected ? "is-selected" : "",
                active && active !== node.name && !related(model, active, node.name)
                  ? "is-dim"
                  : "",
              ].join(" ");
              return (
                <li
                  key={node.name}
                  className="flow-slot"
                  style={{ left: box.x, top: box.y, width: box.width, height: box.height }}
                >
                  <button
                    type="button"
                    ref={(el) => {
                      if (el) buttons.current.set(node.name, el);
                      else buttons.current.delete(node.name);
                    }}
                    className={classes}
                    aria-label={nodeName(
                      node,
                      node.name === current,
                      Boolean(current) && allowed.has(node.name),
                    )}
                    aria-pressed={node.name === selected}
                    aria-current={node.name === current ? "step" : undefined}
                    aria-describedby={wordsId(node.name)}
                    onClick={() => {
                      choose(node.name === selected ? null : node.name);
                    }}
                    onKeyDown={(event) => {
                      move(event, index);
                    }}
                    onFocus={() => {
                      setPointed(node.name);
                    }}
                    onBlur={() => {
                      setPointed(null);
                    }}
                    onPointerEnter={() => {
                      setPointed(node.name);
                    }}
                    onPointerLeave={() => {
                      setPointed(null);
                    }}
                  >
                    <NodeFace
                      node={node}
                      current={node.name === current}
                      next={Boolean(current) && allowed.has(node.name)}
                    />
                  </button>
                </li>
              );
            })}
          </ol>
        </div>
      </div>
      <Legend {...kinds} />
      <FlowWords
        model={model}
        idOf={wordsId}
        hidden={words === "hidden"}
        current={current}
        options={options}
        selected={selected}
      />
    </figure>
  );
}

/** Whether a move or an edit's return joins two states, either way. */
function related(model: FlowModel, a: string, b: string): boolean {
  const joined = model.edges.some(
    (e) => (e.from === a && e.to === b) || (e.from === b && e.to === a),
  );
  const any = model.nodes.some((n) => (n.name === a || n.name === b) && n.fromAny);
  return joined || any;
}

/** A state's button's name: its label first, as it shows (WCAG 2.5.3), then what its
 * marks say. */
function nodeName(node: FlowNode, current: boolean, next: boolean): string {
  const marks = [
    node.initial ? "where records start" : "",
    node.locks ? "locks it" : "",
    current ? "now" : "",
    next ? "you can set it" : "",
  ].filter(Boolean);
  const signed = node.signatures.length
    ? `, signed: ${node.signatures.map(signatureName).join(" and ")}`
    : "";
  const who = node.setters.length ? `Set by ${whoSets(node)}` : "No one sets it";
  const where = node.fromAny ? " from any state" : "";
  return `${[node.label, ...marks].join(", ")}. ${who}${where}${signed}`;
}

function NodeFace({ node, current, next }: { node: FlowNode; current: boolean; next: boolean }) {
  return (
    <>
      <span className="flow-node-head">
        {node.initial ? <span className="flow-start" title="New records start here" /> : null}
        <span className="flow-node-label">{node.label}</span>
        {node.locks ? (
          <span className="flow-lock" title="Locks the record">
            <LockIcon />
          </span>
        ) : null}
        {current ? <span className="flow-tag">Now</span> : null}
        {next ? <span className="flow-tag flow-tag-next">You can</span> : null}
      </span>
      <span className="flow-node-meta">
        <span className="flow-node-who">
          {whoSets(node)}
          {node.fromAny ? " · from any state" : ""}
        </span>
        {node.signatures.length ? (
          <span className="flow-node-sign">
            <PenIcon />
            {node.signatures.map(signatureName).join(" + ")}
          </span>
        ) : null}
      </span>
    </>
  );
}

function Legend({
  move,
  correction,
  any,
  start,
  locks,
  signed,
}: Record<"move" | "correction" | "any" | "start" | "locks" | "signed", boolean>) {
  return (
    <ul className="flow-legend" aria-label="Key">
      {start ? (
        <li>
          <span className="flow-start" aria-hidden="true" /> Where records start
        </li>
      ) : null}
      {locks ? (
        <li>
          <LockIcon /> Locks the record
        </li>
      ) : null}
      {signed ? (
        <li>
          <PenIcon /> Set by an electronic signature, with its meaning
        </li>
      ) : null}
      {move ? (
        <li>
          <svg className="flow-key" aria-hidden="true" width="28" height="10" viewBox="0 0 28 10">
            <path className="flow-edge-line" d="M1 5H22" />
            <path className="flow-edge-head" d="M27 5L21 8.2V1.8Z" />
          </svg>{" "}
          Someone moves it
        </li>
      ) : null}
      {correction ? (
        <li className="flow-edge-correction">
          <svg className="flow-key" aria-hidden="true" width="28" height="10" viewBox="0 0 28 10">
            <path className="flow-edge-line" d="M1 5H22" />
            <path className="flow-edge-head" d="M27 5L21 8.2V1.8Z" />
          </svg>{" "}
          Correcting a value returns it
        </li>
      ) : null}
      {any ? (
        <li className="flow-edge-any">
          <svg className="flow-key" aria-hidden="true" width="28" height="10" viewBox="0 0 28 10">
            <circle className="flow-any-dot" cx="4" cy="5" r="3.5" />
            <path className="flow-edge-line" d="M7 5H22" />
            <path className="flow-edge-head" d="M27 5L21 8.2V1.8Z" />
          </svg>{" "}
          From any other state
        </li>
      ) : null}
    </ul>
  );
}

/** The flow in words: each state, what it means, and how a record comes to it, with its
 * guards' messages. It's what each state's button is described by. */
function FlowWords({
  model,
  idOf,
  hidden,
  current,
  options,
  selected,
}: {
  model: FlowModel;
  idOf: (name: string) => string;
  hidden: boolean;
  current: string | undefined;
  options: readonly FlowOption[] | undefined;
  selected: string | null;
}) {
  const next = current ? new Set(nextFrom(model, current).map((n) => n.name)) : null;
  return (
    <ol
      className={hidden ? "visually-hidden" : "flow-words"}
      aria-label={`${model.label}’s states in words`}
    >
      {model.nodes.map((node) => {
        const option = options?.find((o) => o.name === node.name);
        const yours =
          node.name === current
            ? "It’s in this state now."
            : option?.allowed
              ? "You can set it to this now."
              : option?.why_not && next?.has(node.name)
                ? `You can’t set it to this now: ${option.why_not}`
                : "";
        return (
          <li
            key={node.name}
            id={idOf(node.name)}
            className={node.name === selected ? "is-selected" : undefined}
          >
            <span className="flow-words-title">{node.label}.</span>{" "}
            {(node.initial
              ? [whatItMeans(node, model), wayIn(node, model), yours]
              : [wayIn(node, model), whatItMeans(node, model), yours]
            )
              .filter(Boolean)
              .join(" ")}
            {node.description ? (
              <span className="flow-words-about"> {node.description}</span>
            ) : null}
            {node.signatures.some((s) => s.statement) ? (
              <ul className="flow-words-checks" aria-label="What its signers attest">
                {node.signatures
                  .filter((s) => s.statement)
                  .map((s) => (
                    <li key={s.label}>
                      {signatureName(s)}: “{s.statement}”
                    </li>
                  ))}
              </ul>
            ) : null}
            {node.checks.length ? (
              <ul className="flow-words-checks" aria-label="Its checks">
                {node.checks.map((c) => (
                  <li key={c.message}>{c.message}</li>
                ))}
              </ul>
            ) : null}
          </li>
        );
      })}
    </ol>
  );
}

function LockIcon() {
  return (
    <svg className="flow-icon" aria-hidden="true" viewBox="0 0 16 16" width="12" height="12">
      <path
        fill="currentColor"
        d="M8 1a3.5 3.5 0 0 0-3.5 3.5V6H4a1.5 1.5 0 0 0-1.5 1.5v6A1.5 1.5 0 0 0 4 15h8a1.5 1.5 0 0 0 1.5-1.5v-6A1.5 1.5 0 0 0 12 6h-.5V4.5A3.5 3.5 0 0 0 8 1Zm2 5H6V4.5a2 2 0 1 1 4 0V6Z"
      />
    </svg>
  );
}

function PenIcon() {
  return (
    <svg className="flow-icon" aria-hidden="true" viewBox="0 0 16 16" width="12" height="12">
      <path
        fill="currentColor"
        d="M11.7 1.3a1 1 0 0 1 1.4 0l1.6 1.6a1 1 0 0 1 0 1.4l-8.2 8.2-3.5 1 1-3.5 7.7-8.7ZM1 15h14v-1.2H1V15Z"
      />
    </svg>
  );
}
