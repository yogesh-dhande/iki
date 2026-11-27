/**
 * A saved view's page (iki/core.View, ADR 0086, ADR 0110): its entities, each as it was at the
 * revision the view pinned, in their viewers, as the view saved them. Structures share one
 * Mol* scene; every other entity has a viewer of its own. It says when an entity has changed
 * since, and a switch shows the latest revisions instead. Saving is explicit, and each save is
 * a revision of the view: what the viewers show now, on the revisions they show.
 */

import { unwrap } from "@ikisci/sdk";
import { useQueries } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useCallback, useEffect, useId, useMemo, useRef, useState, type ReactNode } from "react";

import { iki, problemOf, useApiMutation } from "../api";
import {
  ENTITIES,
  recordQuery,
  recordSearch,
  revisionQuery,
  type Entity,
  type EntityPageSearch,
  type EntityType,
} from "../entities";
import { Button, Loading, ProblemAlert, useMessages } from "../ui";
import {
  readViewState,
  viewTargets,
  type Capture,
  type TargetState,
  type ViewState,
  type ViewTarget,
} from "../views";
import { ViewerBody, type Source } from "./viewer-body";
import { StructureScene, type SceneItem } from "./structure-viewer";

type AnyViewer = EntityType["viewers"][number];

/** One of the view's entities, as the page can show it. */
interface Shown {
  index: number;
  target: ViewTarget;
  hrid: string;
  title: string;
  label: string | undefined;
  type: EntityType | undefined;
  viewer: AnyViewer | undefined;
  /** What it shows of it, once it's read. */
  source: Source | undefined;
  /** Why it isn't shown, if it isn't. */
  problem: string | null;
  /** Its link's `?project=`: the view's project's record of a registry entry (ADR 0115). */
  search: EntityPageSearch;
}

/** What the page shows: a viewer for one entity, or Mol*'s scene for its structures. */
interface Panel {
  key: string;
  scene: boolean;
  shown: Shown[];
}

function panelsOf(shown: Shown[]): Panel[] {
  const panels: Panel[] = [];
  let scene: Panel | undefined;
  for (const s of shown) {
    if (s.target.viewer !== "structure") {
      panels.push({ key: `one-${String(s.index)}`, scene: false, shown: [s] });
    } else if (scene) {
      scene.shown.push(s);
    } else {
      scene = { key: "scene", scene: true, shown: [s] };
      panels.push(scene);
    }
  }
  return panels;
}

export function SavedViewPanel({ view, types }: { view: Entity; types: EntityType[] }) {
  const targets = useMemo(() => viewTargets(view), [view]);
  const state = useMemo(
    () => readViewState(view.values["state"], targets.length),
    [view.values, targets.length],
  );
  const [latest, setLatest] = useState(false);
  const switchId = useId();
  const [captures] = useState(() => new Map<string, Capture>());
  const readable = (t: ViewTarget) => {
    const ref = view.refs[t.id];
    return Boolean(ref && !ref.purged);
  };
  const pinned = useQueries({
    queries: targets.map((t) => ({
      // A target of a type with an identity is its registry entry: the view pins its
      // own project's record of it (ADR 0115).
      ...revisionQuery(t.id, t.revision, view.owner_project?.id),
      enabled: readable(t),
    })),
  });
  const current = useQueries({
    queries: targets.map((t) => ({
      ...recordQuery(t.id, view.owner_project?.id),
      enabled: readable(t),
    })),
  });
  const shown: Shown[] = targets.map((target, index) => {
    const ref = view.refs[target.id];
    const type = types.find((t) => t.name === ref?.type);
    const viewer = type?.viewers.find((v) => v.viewer === target.viewer);
    const revision = pinned[index];
    const now = current[index]?.data;
    const source: Source | undefined = latest
      ? now
      : revision?.data
        ? {
            title: ref?.title ?? target.id,
            values: revision.data.values,
            files: revision.data.files,
            refs: revision.data.refs,
            owner_project: view.owner_project,
          }
        : undefined;
    const hrid = ref?.hrid ?? target.id;
    let problem: string | null = null;
    if (ref?.purged) problem = `${hrid} was purged, with the rest of its plugin’s data.`;
    else if (!viewer) problem = `${hrid}’s type doesn’t have the ${target.viewer} viewer any more.`;
    else if (!latest && revision?.isError)
      problem = `Revision ${String(target.revision)} of ${hrid} isn’t one you can read.`;
    return {
      index,
      target,
      hrid,
      title: ref?.title ?? hrid,
      label: state.targets[index]?.label,
      type,
      viewer,
      source,
      problem,
      search: now ? recordSearch(now) : {},
    };
  });

  const { say } = useMessages();
  const save = useApiMutation(async () => {
    const kept: TargetState[] = [...state.targets];
    let camera = state.camera;
    for (const panel of panelsOf(shown)) {
      const capture = captures.get(panel.key);
      if (!capture) continue;
      const now = await capture();
      if (panel.scene) camera = now.camera;
      panel.shown.forEach((s, i) => {
        // The page keeps what a viewer doesn't, such as each entity's label.
        const label = kept[s.index]?.label;
        kept[s.index] = { ...(label ? { label } : {}), ...now.targets[i] };
      });
    }
    const revisions = targets.map(
      (t, i) => (latest ? current[i]?.data?.revision : undefined) ?? t.revision,
    );
    const saved: ViewState = { version: 2, ...(camera ? { camera } : {}), targets: kept };
    return unwrap(
      await iki.PATCH("/api/v1/entities/{ref}", {
        params: {
          path: { ref: view.id },
          header: { "If-Match": `"${String(view.revision)}"` },
        },
        body: { values: { state: saved, target_revisions: revisions }, reason: "" },
      }),
    );
  }, [ENTITIES, ["views"]]);

  if (!targets.length || targets.some((t) => !view.refs[t.id])) {
    return (
      <section className="card">
        <p className="muted">It shows an entity you can’t read.</p>
      </section>
    );
  }
  const behind = targets.some((t, i) => {
    const now = current[i]?.data?.revision;
    return now !== undefined && now !== t.revision;
  });
  const [one] = shown.length === 1 ? shown : [];
  const plural = one ? "" : "s";

  const pin = (s: Shown): ReactNode => {
    if (view.refs[s.target.id]?.purged) return null;
    const now = current[s.index]?.data?.revision;
    const saved = s.target.revision;
    return (
      <>
        <span>
          {latest && now !== undefined
            ? `The latest revision of ${s.hrid}, ${String(now)}. The view saved revision ${String(saved)}.`
            : `Revision ${String(saved)} of ${s.hrid}, as the view saved it.`}
        </span>
        {!latest && now !== undefined && now !== saved ? (
          <span className="view-moved"> It’s changed since: it’s at revision {now} now.</span>
        ) : null}
      </>
    );
  };

  return (
    <section className="card" aria-labelledby="view-title">
      <h2 className="section-title" id="view-title">
        {one ? (
          <>
            {one.viewer?.label ?? "View"} of{" "}
            <Link to="/entities/$ref/" params={{ ref: one.hrid }} search={one.search}>
              {one.title}
            </Link>
          </>
        ) : (
          `${String(shown.length)} entities`
        )}
      </h2>
      <div className="view-pin">
        {one ? (
          <p className="muted">{pin(one)}</p>
        ) : (
          <ol className="view-shows" aria-label="What it shows">
            {shown.map((s) => (
              <li key={`${s.target.id} ${s.target.viewer}`}>
                <Link to="/entities/$ref/" params={{ ref: s.hrid }} search={s.search}>
                  {s.label ?? s.title}
                </Link>{" "}
                <span className="muted">
                  {s.hrid} in the {s.viewer?.label.toLowerCase() ?? s.target.viewer} viewer ·{" "}
                  {pin(s)}
                </span>
              </li>
            ))}
          </ol>
        )}
        <label className="choice" htmlFor={switchId}>
          <input
            id={switchId}
            type="checkbox"
            role="switch"
            checked={latest}
            onChange={(event) => {
              setLatest(event.target.checked);
            }}
          />
          <span className="choice-text">Show the latest revision{plural}</span>
        </label>
        {view.can.edit ? (
          <Button
            busy={save.isPending}
            busyText="Saving…"
            onClick={() => {
              save.mutate(undefined, {
                onSuccess: (saved) => {
                  say(`Saved ${saved.hrid}, revision ${String(saved.revision)}.`);
                },
              });
            }}
          >
            {latest && behind ? `Save on the latest revision${plural}` : "Save the view"}
          </Button>
        ) : null}
      </div>
      <ProblemAlert problem={save.error ? problemOf(save.error) : undefined} />
      {panelsOf(shown).map((panel) => (
        <PanelBody
          key={`${panel.key} ${latest ? "latest" : "pinned"}`}
          panel={panel}
          state={state}
          titled={!one}
          viewTitle={view.title}
          captures={captures}
        />
      ))}
    </section>
  );
}

/** One panel of a view: its entities' viewer, once what it shows of them is read. */
/** `state`, with what it's superposed on renumbered by `to`, or not kept if `to` has none. */
function reindexed(state: TargetState, to: (onto: number) => number | undefined): TargetState {
  if (!state.superposed) return state;
  const { superposed, ...rest } = state;
  const onto = to(superposed.onto);
  return onto === undefined ? rest : { ...rest, superposed: { ...superposed, onto } };
}

function PanelBody({
  panel,
  state,
  titled,
  viewTitle,
  captures,
}: {
  panel: Panel;
  state: ViewState;
  /** Whether it's one of several, which says which entity it shows. */
  titled: boolean;
  viewTitle: string;
  captures: Map<string, Capture>;
}) {
  const notes = panel.shown.flatMap((s) =>
    s.problem
      ? [
          <p key={s.index} className="muted">
            {s.problem}
          </p>,
        ]
      : [],
  );
  const drawn = panel.shown.filter((s) => !s.problem);
  // What it shows as of its latest render, for saving: its viewer captures once.
  const now = useRef({ panel, state });
  useEffect(() => {
    now.current = { panel, state };
  });
  const { key } = panel;
  const onCapture = useCallback(
    (fn: Capture | null) => {
      if (!fn) {
        captures.delete(key);
        return;
      }
      captures.set(key, async () => {
        const captured = await fn();
        // Entities it doesn't draw keep what the view saved of them.
        const { panel: shownNow, state: savedNow } = now.current;
        const drawnNow = shownNow.shown.filter((s) => !s.problem);
        const targets = shownNow.shown.map((s) => {
          const at = drawnNow.indexOf(s);
          // The scene numbers what it superposed on among what it draws; the view, among
          // all it shows.
          return at >= 0
            ? reindexed(captured.targets[at] ?? {}, (onto) => drawnNow[onto]?.index)
            : (savedNow.targets[s.index] ?? {});
        });
        return { ...captured, targets };
      });
    },
    [key, captures],
  );
  let body: ReactNode = null;
  if (drawn.some((s) => !s.source)) {
    body = <Loading label="Opening the view…" />;
  } else if (panel.scene) {
    const items: SceneItem[] = drawn.flatMap((s) =>
      s.source && s.type && s.viewer?.viewer === "structure"
        ? [
            {
              key: `${s.target.id} ${String(s.index)}`,
              source: s.source,
              type: s.type,
              viewer: s.viewer,
              state: reindexed(state.targets[s.index] ?? {}, (onto) => {
                const at = drawn.findIndex((d) => d.index === onto);
                return at >= 0 ? at : undefined;
              }),
              hrid: s.hrid,
            },
          ]
        : [],
    );
    const [first] = items;
    if (first) {
      body = (
        <StructureScene
          items={items}
          title={
            items.length === 1
              ? `${first.viewer.label} of ${first.source.title}`
              : `Structures of ${viewTitle}`
          }
          camera={state.camera}
          onCapture={onCapture}
        />
      );
    }
  } else {
    const [only] = drawn;
    if (only?.source && only.type && only.viewer) {
      body = (
        <ViewerBody
          source={only.source}
          type={only.type}
          viewer={only.viewer}
          state={state.targets[only.index]}
          onCapture={onCapture}
        />
      );
    }
  }
  const [heading] = titled && !panel.scene ? panel.shown : [];
  return (
    <div className="view-panel">
      {heading ? (
        <h3 className="view-panel-title">
          {heading.viewer?.label ?? heading.target.viewer} of {heading.label ?? heading.title}
        </h3>
      ) : null}
      {notes}
      {body}
    </div>
  );
}
