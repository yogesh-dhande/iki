/**
 * A tab a plugin adds to an entity's page: its component, in a sandboxed frame (ADR 0084).
 */

import { useSuspenseQuery } from "@tanstack/react-query";
import { useParams } from "@tanstack/react-router";

import { ComponentFrame } from "../component-frame";
import { componentsQuery, entityTabs } from "../components";
import { usePageTitle } from "../layout";
import { EntityTabs, useEntity } from "./entity";
import { NotFound } from "./errors";

export function EntityComponent() {
  const { entity, type, types } = useEntity();
  const { name } = useParams({ from: "/entities/$ref/components/$name" });
  const components = useSuspenseQuery(componentsQuery).data;
  const tab = entityTabs(entity.type, types, components).find((t) => t.component.name === name);
  usePageTitle(tab ? `${tab.label} of ${entity.title} (${entity.hrid})` : undefined);
  if (!tab) return <NotFound />;
  return (
    <>
      <EntityTabs entity={entity} type={type} />
      {tab.component.description ? <p className="muted">{tab.component.description}</p> : null}
      <ComponentFrame component={tab.component} label={tab.label} entity={entity} />
    </>
  );
}
