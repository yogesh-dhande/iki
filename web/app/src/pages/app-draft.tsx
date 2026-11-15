/**
 * A draft of an app (ADR 0090), to look at before saving it: what an agent drafted for
 * its person, say. Only that person can open it, for a day. It shows as the app would,
 * with what they may read, and they can save it as an app in the project.
 */

import { unwrap } from "@ikisci/sdk";
import { useQuery } from "@tanstack/react-query";
import { useNavigate, useParams } from "@tanstack/react-router";

import { iki, problemOf, useApiMutation } from "../api";
import { APP_TYPE, appDraftQuery } from "../apps";
import { ENTITIES, entityQuery, typesQuery } from "../entities";
import { usePageTitle } from "../layout";
import { Button, Loading, ProblemAlert, RelativeTime, useMessages } from "../ui";
import { AppPanel } from "./app";
import { useProject } from "./project-overview";

export function AppDraft() {
  const project = useProject();
  const { draft: id } = useParams({ from: "/projects/$slug/apps/drafts/$draft" });
  const draft = useQuery(appDraftQuery(id));
  const about = draft.data?.entity;
  const entity = useQuery({ ...entityQuery(about?.hrid ?? ""), enabled: Boolean(about) });
  const types = useQuery(typesQuery);
  const navigate = useNavigate();
  const { say } = useMessages();
  const save = useApiMutation(
    async () =>
      unwrap(
        await iki.POST("/api/v1/entities", {
          body: {
            type: APP_TYPE,
            project: project.id,
            values: { name: draft.data?.name, spec: draft.data?.spec },
            allow_duplicate: false,
            reason: draft.data?.agent ? `Saved from a draft ${draft.data.agent} made.` : "",
          },
        }),
      ),
    [ENTITIES],
  );
  usePageTitle(draft.data ? `Draft: ${draft.data.name} · ${project.name}` : "Draft");

  if (draft.isError) {
    return problemOf(draft.error).status === 404 ? (
      <section className="card">
        <h2 className="section-title">This draft can’t be opened</h2>
        <p>
          It’s expired, or it isn’t yours. A draft lasts a day, and only the person it was made for
          can open it. Ask for it again.
        </p>
      </section>
    ) : (
      <ProblemAlert problem={problemOf(draft.error)} />
    );
  }
  const problem = entity.error ?? types.error;
  if (problem) return <ProblemAlert problem={problemOf(problem)} />;
  if (!draft.data || !types.data || (about && !entity.data)) {
    return <Loading label="Opening the draft…" />;
  }
  const { name, spec, agent, expires_at } = draft.data;
  const source = {
    id: draft.data.id,
    values: { name, spec },
    owner_project: { id: project.id, name: project.name, slug: project.slug },
  };
  return (
    <section className="dashboard" aria-labelledby="draft-title">
      <div className="alert alert-info" role="note">
        <p>
          <strong>A draft, not saved.</strong> {agent ? `${agent} drafted it for you. ` : null}
          Only you can see it, until <RelativeTime value={expires_at} />.
          {entity.data ? ` It’s shown on ${entity.data.title} (${entity.data.hrid}).` : null}
        </p>
        {project.can.edit && !project.archived ? (
          <>
            <ProblemAlert problem={save.error ? problemOf(save.error) : undefined} />
            <Button
              variant="primary"
              busy={save.isPending}
              busyText="Saving…"
              onClick={() => {
                save.mutate(undefined, {
                  onSuccess: (made) => {
                    void navigate({ to: "/entities/$ref/", params: { ref: made.hrid } }).then(
                      () => {
                        say(`Saved ${name} as ${made.hrid}.`);
                      },
                    );
                  },
                });
              }}
            >
              Save as an app in {project.name}
            </Button>
          </>
        ) : null}
      </div>
      <div className="dashboard-heading">
        <h2 className="section-title" id="draft-title">
          {name}
        </h2>
      </div>
      <AppPanel app={source} types={types.data} {...(entity.data ? { about: entity.data } : {})} />
    </section>
  );
}
