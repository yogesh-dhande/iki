/**
 * A workflow's steps as a graph (ADR 0079): each step one column right of the steps it
 * waits for, with a line from each to what waits for it. On a workflow run's page each
 * shows how far it has got and links to its run, and follows it live as the page does.
 */

import { Link } from "@tanstack/react-router";
import { useId, useMemo, useState, type ReactNode } from "react";

import { arrange, connected, NODE_HEIGHT, NODE_WIDTH } from "../dag";

export interface GraphStep {
  id: string;
  label: string;
  after: string[];
  /** What it runs, or how far it has got, under its label. */
  meta: string;
  /** A badge tone (good, bad, busy, quiet), for a step of a run. */
  tone?: string;
  /** Its run or batch, once it has one. */
  link?: { to: "/runs/$ref/" | "/batches/$ref/"; ref: string } | undefined;
}

interface Pointing {
  onMouseEnter: () => void;
  onMouseLeave: () => void;
  onFocus: () => void;
  onBlur: () => void;
}

function StepNode({
  step,
  className,
  point,
}: {
  step: GraphStep;
  className: string;
  point: Pointing;
}): ReactNode {
  const classes = `lineage-node workflow-node ${step.tone ? `tone-${step.tone}` : ""} ${className}`;
  const body = (
    <>
      <span className="lineage-title">{step.label}</span>
      <span className="lineage-meta">{step.meta}</span>
    </>
  );
  return step.link ? (
    <Link to={step.link.to} params={{ ref: step.link.ref }} className={classes} {...point}>
      {body}
    </Link>
  ) : (
    <div className={classes} {...point}>
      {body}
    </div>
  );
}

export function StepGraph({ steps, caption }: { steps: GraphStep[]; caption: string }) {
  const shape = useMemo(
    () =>
      arrange(
        steps.map((s) => s.id),
        steps.flatMap((s) => s.after.map((a) => [a, s.id] as const)),
      ),
    [steps],
  );
  const byId = useMemo(() => new Map(steps.map((s) => [s.id, s])), [steps]);
  const [lit, setLit] = useState<string | null>(null);
  const on = useMemo(() => (lit ? connected(shape.edges, lit) : null), [lit, shape]);
  const arrow = useId();
  const captionId = useId();
  const point = (key: string): Pointing => ({
    onMouseEnter: () => {
      setLit(key);
    },
    onMouseLeave: () => {
      setLit(null);
    },
    onFocus: () => {
      setLit(key);
    },
    onBlur: () => {
      setLit(null);
    },
  });
  const state = (key: string) => (on ? (on.has(key) ? "is-lit" : "is-dim") : "");
  return (
    <figure className="lineage workflow-graph">
      <figcaption className="muted" id={captionId}>
        {caption}
      </figcaption>
      {/* A scrolling region people reach with Tab, so the keyboard scrolls it. */}
      {/* eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- a scrollable region */}
      <div className="lineage-scroll" tabIndex={0} role="group" aria-labelledby={captionId}>
        <div
          className="lineage-canvas"
          style={{ width: `${String(shape.width)}rem`, height: `${String(shape.height)}rem` }}
        >
          <svg
            className="lineage-lines"
            aria-hidden="true"
            focusable="false"
            viewBox={`0 0 ${String(shape.width)} ${String(shape.height)}`}
            preserveAspectRatio="none"
          >
            <defs>
              <marker
                id={arrow}
                viewBox="0 0 10 10"
                refX="10"
                refY="5"
                markerWidth="7"
                markerHeight="7"
                markerUnits="userSpaceOnUse"
                orient="auto-start-reverse"
              >
                <path className="lineage-arrow" d="M 0 0 L 10 5 L 0 10 z" />
              </marker>
            </defs>
            {shape.edges.map((e) => (
              <path
                key={`${e.from}>${e.to}`}
                className={`lineage-line ${on ? (on.has(e.from) && on.has(e.to) ? "is-lit" : "is-dim") : ""}`}
                d={e.path}
                vectorEffect="non-scaling-stroke"
                markerEnd={`url(#${arrow})`}
              />
            ))}
          </svg>
          {shape.places.map((p) => {
            const step = byId.get(p.key);
            return step ? (
              <div
                key={p.key}
                className="lineage-place"
                style={{
                  left: `${String(p.x)}rem`,
                  top: `${String(p.y)}rem`,
                  width: `${String(NODE_WIDTH)}rem`,
                  height: `${String(NODE_HEIGHT)}rem`,
                }}
              >
                <StepNode step={step} className={state(p.key)} point={point(p.key)} />
              </div>
            ) : null;
          })}
        </div>
      </div>
    </figure>
  );
}
