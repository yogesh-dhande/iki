/**
 * A plugin's component in one of its places (ADR 0084, ADR 0117): an entity's tab, the
 * grid's results, a project's page, a person's home or an app's panel. The page gives its
 * frame what the place shows, as the person may read it; what they pick in it is read
 * back from the API and offered to Iki's run page, and an entity or a pinned revision it
 * opens is opened by Iki.
 */

import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate } from "@tanstack/react-router";
import { useCallback, useEffect, useMemo, useState } from "react";

import { problemOf } from "../api";
import type { PlaceData } from "../component-entities";
import { ComponentFrame, type GivenEntity } from "../component-frame";
import type { PinnedRevision, PlaceInfo, PluginComponent } from "../components";
import { inSentence, type Entity, type EntityType } from "../entities";
import { entitiesByHridQuery, MAX_BATCH } from "../runs";
import { buttonClass, ProblemAlert } from "../ui";

/** The project a run on what's picked would be in: the place's, or, on a person's home,
 * the one every picked entity is in. */
export interface RunProject {
  slug: string;
}

/** Nothing picked: the same each render. */
const NONE: Entity[] = [];

export function PluginPlace({
  component,
  label,
  place,
  entity,
  data,
  project,
  types,
  onPicked,
  onRun,
  onFailed,
}: {
  component: PluginComponent;
  label: string;
  place: PlaceInfo;
  entity?: GivenEntity | undefined;
  data?: PlaceData | undefined;
  /** The project a run starts in; on a person's home, null: each pick's own. */
  project: RunProject | null;
  types: EntityType[];
  /** What's picked, as read back: an app's panel offers it to the panels after it. */
  onPicked?: ((entities: Entity[]) => void) | undefined;
  /** A person asked to run one of the actions its place offers (ADR 0170). */
  onRun?: ((action: string) => void) | undefined;
  onFailed?: (() => void) | undefined;
}) {
  const [picked, setPicked] = useState<string[]>([]);
  const navigate = useNavigate();
  const onSelect = useCallback((hrids: string[]) => {
    setPicked(hrids);
  }, []);
  const items = data?.entities.items;
  const onOpen = useCallback(
    (hrid: string) => {
      const item = items?.find((e) => e.hrid.toUpperCase() === hrid);
      const slug = item?.project?.slug;
      void navigate({
        to: "/entities/$ref/",
        params: { ref: hrid },
        search: slug ? { project: slug } : {},
      });
    },
    [items, navigate],
  );
  // A revision its entity pins, as evidence (ADR 0189): that revision's page, in Iki.
  const onOpenRevision = useCallback(
    (pin: PinnedRevision) => {
      const params = { ref: pin.id, number: String(pin.revision) };
      void navigate(
        pin.of === "entry"
          ? { to: "/entries/$ref/revisions/$number/", params }
          : { to: "/entities/$ref/revisions/$number/", params },
      );
    },
    [navigate],
  );
  return (
    <>
      {data?.problem ? <ProblemAlert problem={data.problem} /> : null}
      {component.selects ? (
        <Selection
          component={component}
          types={types}
          hrids={picked}
          project={project}
          onPicked={onPicked}
          onClear={() => {
            setPicked([]);
          }}
        />
      ) : null}
      <ComponentFrame
        component={component}
        label={label}
        place={place}
        entity={entity}
        entities={data?.entities}
        onSelect={onSelect}
        onMore={data?.more}
        onRefine={data?.refine}
        onOpen={onOpen}
        onOpenRevision={onOpenRevision}
        onRun={onRun}
        onFailed={onFailed}
      />
    </>
  );
}

/** What a person picked in the component: read back from the API as they may read it,
 * and only of the type it declared, so the component can't offer anything else. */
function Selection({
  component,
  types,
  hrids,
  project,
  onPicked,
  onClear,
}: {
  component: PluginComponent;
  types: EntityType[];
  hrids: string[];
  project: RunProject | null;
  onPicked: ((entities: Entity[]) => void) | undefined;
  onClear: () => void;
}) {
  const declared = types.find((t) => t.name === component.selects);
  const tooMany = hrids.length > MAX_BATCH;
  const looked = hrids.length > 0 && !tooMany;
  const found = useQuery({ ...entitiesByHridQuery(hrids), enabled: looked });
  const kept = useMemo(() => {
    if (!looked || !found.data) return NONE;
    const allowed = new Set([component.selects, ...(declared?.subtypes ?? [])]);
    return found.data.filter((e) => allowed.has(e.type));
  }, [looked, found.data, component.selects, declared]);
  useEffect(() => {
    onPicked?.(kept);
  }, [kept, onPicked]);
  const left = looked && found.data ? hrids.length - kept.length : 0;
  const label = declared?.label ?? "entity";
  const plural = declared?.label_plural ?? "entities";
  // On a person's home, a run is in the one project everything picked is in.
  const projects = [...new Set(kept.map((e) => e.owner_project?.slug ?? ""))];
  const runIn = project ?? (projects.length === 1 && projects[0] ? { slug: projects[0] } : null);

  let status = "";
  if (tooMany) {
    status = `${String(hrids.length)} selected: an action runs on at most ${String(MAX_BATCH)} at once`;
  } else if (looked && !found.data) {
    status = `${String(hrids.length)} selected`;
  } else if (looked) {
    status = `${String(kept.length)} ${inSentence(kept.length === 1 ? label : plural)} selected`;
    if (left) {
      status += `. ${String(left)} left out: not ${inSentence(plural)} you can read`;
    }
    if (kept.length && !runIn) {
      status += `, from more than one project: pick from one to run an action on them`;
    }
  }
  return (
    <div className={hrids.length ? "selection-bar" : undefined}>
      {/* The count stays in the page, so screen readers hear it change as entities are picked. */}
      <p role="status" className={hrids.length ? undefined : "visually-hidden"}>
        {status}
      </p>
      {found.error ? <ProblemAlert problem={problemOf(found.error)} /> : null}
      {kept.length && runIn ? (
        <Link
          to="/projects/$slug/runs/new/"
          params={{ slug: runIn.slug }}
          search={{ entities: kept.map((e) => e.hrid).join(",") }}
          className={buttonClass("primary")}
        >
          Run action on {kept.length === 1 ? "it" : "them"}
        </Link>
      ) : null}
      {hrids.length ? (
        <button type="button" className={buttonClass("quiet")} onClick={onClear}>
          Clear selection
        </button>
      ) : null}
    </div>
  );
}
