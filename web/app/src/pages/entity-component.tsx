/**
 * A tab a plugin adds to an entity's page: its component, in a sandboxed frame (ADR 0084),
 * given the entity and, if its tab declares a search, what that finds about it in its
 * project (ADR 0117), and what a person picks in it, ready to run an action on. Of the
 * actions it offers to run on the entity (ADR 0170), its element is told those the person
 * may run here now, and one they ask for opens Iki's run page with it and the entity
 * chosen, where they check and start it.
 */

import type { Place } from "@ikisci/component";
import { useQuery, useSuspenseQuery } from "@tanstack/react-query";
import { useNavigate, useParams } from "@tanstack/react-router";
import { useCallback, useMemo } from "react";

import { projectQuery } from "../api";
import { usePlaceEntities } from "../component-entities";
import {
  componentsQuery,
  entityTabs,
  lineageOf,
  type EntityTab,
  type PluginComponent,
} from "../components";
import type { Entity, EntityType } from "../entities";
import { usePageTitle } from "../layout";
import { actionsQuery } from "../runs";
import { useEntity } from "./entity-header";
import { NoSuchTab } from "./errors";
import { PluginPlace } from "./plugin-place";

export function EntityComponent() {
  const { entity, types } = useEntity();
  const { _splat: name = "" } = useParams({ from: "/entities/$ref/components/$" });
  const components = useSuspenseQuery(componentsQuery).data;
  const tab = entityTabs(entity.type, types, components).find((t) => t.component.name === name);
  usePageTitle(tab ? `${tab.label} of ${entity.title} (${entity.hrid})` : undefined);
  if (!tab) return <NoSuchTab />;
  return (
    <>
      {tab.component.description ? <p className="muted">{tab.component.description}</p> : null}
      <Tab key={tab.component.name} tab={tab} entity={entity} types={types} />
    </>
  );
}

/** Nothing offered: the same each render. */
const NONE: string[] = [];

/** Which of the actions the component offers the person may run on the entity here now:
 * none until that's known, and none on an entity that's archived or in no project of
 * theirs. The run page and the server check it all again. */
function useOffered(component: PluginComponent, entity: Entity, types: EntityType[]): string[] {
  const offers = component.runs.length > 0;
  const slug = !entity.archived ? entity.owner_project?.slug : undefined;
  const project = useQuery({ ...projectQuery(slug ?? ""), enabled: offers && Boolean(slug) });
  const actions = useQuery({ ...actionsQuery, enabled: offers && Boolean(slug) });
  return useMemo(() => {
    if (!project.data || !actions.data) return NONE;
    const can = project.data.can;
    const lineage = new Set(lineageOf(entity.type, types));
    return component.runs.filter((name) => {
      const action = actions.data.find((a) => a.name === name);
      if (!action?.available || !can.run.includes(action.tier)) return false;
      const takes = action.inputs.filter((input) => lineage.has(input.type));
      if (!takes.length) return false;
      // An action that changes the entity needs the person to be able to change it.
      const changes = action.updates.some((u) => takes.some((input) => input.name === u.input));
      return !changes || entity.can.edit;
    });
  }, [project.data, actions.data, component.runs, entity.type, entity.can.edit, types]);
}

function Tab({ tab, entity, types }: { tab: EntityTab; entity: Entity; types: EntityType[] }) {
  // The tab's search, about this entity: only for a tab that declares one.
  const many = Boolean(tab.slot.search && tab.component.entities);
  const data = usePlaceEntities(tab.component, many ? { entity: entity.id } : null);
  const offered = useOffered(tab.component, entity, types);
  const offers = tab.component.runs.length > 0;
  const place = useMemo<Place>(
    () => ({ slot: "entity.tab", label: tab.label, ...(offers ? { runs: offered } : {}) }),
    [tab.label, offers, offered],
  );
  const navigate = useNavigate();
  const slug = entity.owner_project?.slug;
  const onRun = useCallback(
    (action: string) => {
      if (!slug) return;
      void navigate({
        to: "/projects/$slug/runs/new/",
        params: { slug },
        search: { action, entities: entity.hrid },
      });
    },
    [navigate, slug, entity.hrid],
  );
  return (
    <PluginPlace
      component={tab.component}
      label={tab.label}
      place={place}
      entity={entity}
      data={many ? data : undefined}
      project={entity.owner_project}
      types={types}
      onRun={onRun}
    />
  );
}
