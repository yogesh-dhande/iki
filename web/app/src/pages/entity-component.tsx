/**
 * A tab a plugin adds to an entity's page: its component, in a sandboxed frame (ADR 0084),
 * given the entity and, if its tab declares a search, what that finds about it in its
 * project (ADR 0117), and what a person picks in it, ready to run an action on.
 */

import { useSuspenseQuery } from "@tanstack/react-query";
import { useParams } from "@tanstack/react-router";

import { usePlaceEntities } from "../component-entities";
import { componentsQuery, entityTabs, type EntityTab } from "../components";
import type { Entity, EntityType } from "../entities";
import { usePageTitle } from "../layout";
import { EntityTabs, useEntity } from "./entity";
import { NotFound } from "./errors";
import { PluginPlace } from "./plugin-place";

export function EntityComponent() {
  const { entity, type, types } = useEntity();
  const { _splat: name = "" } = useParams({ from: "/entities/$ref/components/$" });
  const components = useSuspenseQuery(componentsQuery).data;
  const tab = entityTabs(entity.type, types, components).find((t) => t.component.name === name);
  usePageTitle(tab ? `${tab.label} of ${entity.title} (${entity.hrid})` : undefined);
  if (!tab) return <NotFound />;
  return (
    <>
      <EntityTabs entity={entity} type={type} />
      {tab.component.description ? <p className="muted">{tab.component.description}</p> : null}
      <Tab key={tab.component.name} tab={tab} entity={entity} types={types} />
    </>
  );
}

function Tab({ tab, entity, types }: { tab: EntityTab; entity: Entity; types: EntityType[] }) {
  // The tab's search, about this entity: only for a tab that declares one.
  const many = Boolean(tab.slot.search && tab.component.entities);
  const data = usePlaceEntities(tab.component, many ? { entity: entity.id } : null);
  return (
    <PluginPlace
      component={tab.component}
      label={tab.label}
      place={{ slot: "entity.tab", label: tab.label }}
      entity={entity}
      data={many ? data : undefined}
      project={entity.owner_project}
      types={types}
    />
  );
}
