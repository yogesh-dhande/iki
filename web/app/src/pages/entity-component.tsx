/**
 * A tab a plugin adds to an entity's page: its component, in a sandboxed frame (ADR 0084),
 * and what a person picks in it, ready to run an action on.
 */

import { useQuery, useSuspenseQuery } from "@tanstack/react-query";
import { Link, useParams } from "@tanstack/react-router";
import { useCallback, useState } from "react";

import { ComponentFrame } from "../component-frame";
import { componentsQuery, entityTabs, type PluginComponent } from "../components";
import { inSentence, type Entity, type EntityType } from "../entities";
import { usePageTitle } from "../layout";
import { entitiesByHridQuery, MAX_BATCH } from "../runs";
import { buttonClass } from "../ui";
import { EntityTabs, useEntity } from "./entity";
import { NotFound } from "./errors";

export function EntityComponent() {
  const { entity, type, types } = useEntity();
  const { name } = useParams({ from: "/entities/$ref/components/$name" });
  const components = useSuspenseQuery(componentsQuery).data;
  const tab = entityTabs(entity.type, types, components).find((t) => t.component.name === name);
  const [picked, setPicked] = useState<string[]>([]);
  const onSelect = useCallback((hrids: string[]) => {
    setPicked(hrids);
  }, []);
  usePageTitle(tab ? `${tab.label} of ${entity.title} (${entity.hrid})` : undefined);
  if (!tab) return <NotFound />;
  return (
    <>
      <EntityTabs entity={entity} type={type} />
      {tab.component.description ? <p className="muted">{tab.component.description}</p> : null}
      {tab.component.selects ? (
        <Selection
          component={tab.component}
          entity={entity}
          types={types}
          hrids={picked}
          onClear={() => {
            setPicked([]);
          }}
        />
      ) : null}
      <ComponentFrame
        component={tab.component}
        label={tab.label}
        entity={entity}
        onSelect={onSelect}
      />
    </>
  );
}

/** What a person picked in the component: read back from the API as they may read it,
 * and only of the type it declared, so the component can't offer anything else. */
function Selection({
  component,
  entity,
  types,
  hrids,
  onClear,
}: {
  component: PluginComponent;
  entity: Entity;
  types: EntityType[];
  hrids: string[];
  onClear: () => void;
}) {
  const declared = types.find((t) => t.name === component.selects);
  const allowed = new Set([component.selects, ...(declared?.subtypes ?? [])]);
  const tooMany = hrids.length > MAX_BATCH;
  const found = useQuery({
    ...entitiesByHridQuery(hrids),
    enabled: hrids.length > 0 && !tooMany,
  }).data;
  const kept = found?.filter((e) => allowed.has(e.type)) ?? [];
  const left = found ? hrids.length - kept.length : 0;
  const label = declared?.label ?? "entity";
  const plural = declared?.label_plural ?? "entities";
  const project = entity.owner_project;

  let status = "";
  if (tooMany) {
    status = `${String(hrids.length)} selected: an action runs on at most ${String(MAX_BATCH)} at once`;
  } else if (hrids.length && !found) {
    status = `${String(hrids.length)} selected`;
  } else if (found) {
    status = `${String(kept.length)} ${inSentence(kept.length === 1 ? label : plural)} selected`;
    if (left) {
      status += `. ${String(left)} left out: not ${inSentence(plural)} you can read`;
    }
  }
  return (
    <div className={hrids.length ? "selection-bar" : undefined}>
      {/* The count stays in the page, so screen readers hear it change as entities are picked. */}
      <p role="status" className={hrids.length ? undefined : "visually-hidden"}>
        {status}
      </p>
      {kept.length && project ? (
        <Link
          to="/projects/$slug/runs/new/"
          params={{ slug: project.slug }}
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
