/**
 * An app about an entity (ADR 0090), as a tab on its page: the app's panels, reading
 * that entity as `entity`. Only apps the server lists for it are shown, so an app about
 * another type, or from a project the entity isn't in, can't be opened on its page.
 */

import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "@tanstack/react-router";

import { problemOf } from "../api";
import { entityAppsQuery } from "../apps";
import { apiRef, entityQuery } from "../entities";
import { usePageTitle } from "../layout";
import { Loading, ProblemAlert } from "../ui";
import { AppPanel } from "./app";
import { useEntity } from "./entity-header";
import { NoSuchTab } from "./errors";

export function EntityApp() {
  const { entity, types } = useEntity();
  const { app: ref } = useParams({ from: "/entities/$ref/apps/$app" });
  const listed = useQuery(entityAppsQuery(apiRef(entity)));
  const tab = listed.data?.find((a) => a.hrid === ref.toUpperCase());
  const app = useQuery({ ...entityQuery(ref), enabled: tab !== undefined });
  usePageTitle(tab ? `${tab.name} of ${entity.title} (${entity.hrid})` : undefined);
  let body: React.ReactNode;
  if (listed.isError) body = <ProblemAlert problem={problemOf(listed.error)} />;
  else if (!listed.data) body = <Loading label="Finding its apps…" />;
  else if (!tab) body = <NoSuchTab />;
  else if (app.isError) body = <ProblemAlert problem={problemOf(app.error)} />;
  else if (!app.data) body = <Loading label="Opening the app…" />;
  else {
    const description = app.data.values["description"];
    body = (
      <>
        <p className="muted">
          {typeof description === "string" && description ? `${description} ` : null}
          <Link to="/entities/$ref/" params={{ ref: app.data.hrid }}>
            How it’s built ({app.data.hrid})
          </Link>
        </p>
        <AppPanel app={app.data} types={types} about={entity} />
      </>
    );
  }
  return <>{body}</>;
}
