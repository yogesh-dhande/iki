/**
 * The Export card in a project's settings: a project's Admins export a collection, with every
 * revision of its entities, the files they name and who made them, in a signed bundle for
 * another Iki deployment (ADR 0096). It's written in the background and kept for a week.
 */
import { unwrap, type Schemas } from "@ikisci/sdk";
import { queryOptions, useQuery, type UseQueryResult } from "@tanstack/react-query";
import { useState, type ReactNode, type SubmitEvent } from "react";

import { iki, problemOf, useApiMutation, type Project } from "../api";
import { searchEntities, formatSize } from "../entities";
import { required, useForm } from "../forms";
import {
  Badge,
  Button,
  Loading,
  ProblemAlert,
  RelativeTime,
  SelectField,
  TextField,
  Time,
  useMessages,
} from "../ui";

type Export = Schemas["ExportOut"];

const STATES: Record<Export["state"], string> = {
  queued: "Queued",
  writing: "Writing…",
  ready: "Ready",
  failed: "Failed",
  expired: "Expired",
};

function exportsQuery(projectId: string) {
  return queryOptions({
    queryKey: ["projects", projectId, "bundles"],
    queryFn: async ({ signal }) =>
      unwrap(
        await iki.GET("/api/v1/projects/{project_id}/bundles", {
          params: { path: { project_id: projectId } },
          signal,
        }),
      ).items,
    // Until each is written, look again every few seconds.
    refetchInterval: (query) =>
      query.state.data?.some((e) => e.state === "queued" || e.state === "writing") ? 2000 : false,
  });
}

function collectionsQuery(projectId: string) {
  return queryOptions({
    queryKey: ["entities", "collections", projectId],
    queryFn: async ({ signal }) =>
      unwrap(
        await searchEntities(
          {
            project: projectId,
            type: "iki/core.Collection",
            exact_type: true,
            archived: false,
            limit: 100,
            sort: { field: "updated_at", descending: true },
          },
          signal,
        ),
      ).items,
  });
}

function plural(count: number, one: string, many: string): string {
  return `${count.toLocaleString()} ${count === 1 ? one : many}`;
}

function holds(made: Export): string {
  const c = made.counts;
  if (!c) return "";
  return [
    plural(c.entities, "entity", "entities"),
    plural(c.revisions, "revision", "revisions"),
    plural(c.files, "file", "files"),
  ].join(", ");
}

export function Exports({ project }: { project: Project }) {
  const { say } = useMessages();
  const exports = useQuery(exportsQuery(project.id));
  const collections = useQuery(collectionsQuery(project.id));
  const [collection, setCollection] = useState("");
  const [reason, setReason] = useState("");
  const start = useApiMutation(
    async (body: { collection: string; reason: string }) =>
      unwrap(
        await iki.POST("/api/v1/projects/{project_id}/bundles", {
          params: { path: { project_id: project.id } },
          body,
        }),
      ),
    [["projects", project.id, "bundles"]],
  );
  const form = useForm(start.error);

  function submit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    const checks = {
      collection: required(collection, "Choose the collection to export."),
      reason: required(reason, "Say why you’re exporting it."),
    };
    if (!form.validate(checks)) return;
    start.mutate(
      { collection, reason: reason.trim() },
      {
        onSuccess: (made) => {
          setReason("");
          setCollection("");
          say(`Exporting ${made.selection}. It’s listed below once it’s written.`);
        },
      },
    );
  }

  let picker: ReactNode;
  if (collections.isError) {
    picker = <ProblemAlert problem={problemOf(collections.error)} />;
  } else if (!collections.data) {
    picker = <Loading label="Finding its collections…" />;
  } else if (collections.data.length === 0) {
    picker = (
      <p className="muted">
        Make a collection of what you want to send first: from a search, or by adding entities to
        one.
      </p>
    );
  } else {
    picker = (
      <>
        {form.alert}
        <form {...form.props} onSubmit={submit}>
          <SelectField
            label="Collection"
            value={collection}
            onChange={setCollection}
            options={[
              { value: "", label: "Choose one…" },
              ...collections.data.map((c) => ({
                value: c.hrid,
                label: `${c.title} (${c.hrid})`,
              })),
            ]}
            error={form.errors["collection"]}
            help="Its members go with it, with what they refer to in this project. For a collection kept by a search, what the search finds for you now."
          />
          <TextField
            label="Why"
            value={reason}
            onChange={setReason}
            error={form.errors["reason"]}
            maxLength={2000}
            help="Such as “Validated primers, for production”. The bundle and the audit log keep it."
          />
          <Button type="submit" variant="primary" busy={start.isPending} busyText="Exporting…">
            Export
          </Button>
        </form>
      </>
    );
  }

  return (
    // Its list is a table: it gets the whole row once there's one.
    <section className={exports.data?.length ? "card card-wide" : "card"}>
      <h2 className="section-title">Export</h2>
      <p>
        A bundle carries entities to another Iki deployment, such as from staging to production,
        with every revision, the files they name and who made each change. This deployment signs it,
        and keeps it here for a week.
      </p>
      {picker}
      <ExportList query={exports} />
    </section>
  );
}

function ExportList({ query }: { query: UseQueryResult<Export[]> }) {
  if (query.isError) return <ProblemAlert problem={problemOf(query.error)} />;
  if (!query.data) return <Loading label="Finding its exports…" />;
  if (query.data.length === 0) return null;
  return (
    <table className="table">
      <caption className="visually-hidden">Exports, newest first</caption>
      <thead>
        <tr>
          <th scope="col">Asked for</th>
          <th scope="col">What, and why</th>
          <th scope="col">Bundle</th>
        </tr>
      </thead>
      <tbody>
        {query.data.map((made) => (
          <tr key={made.id}>
            <td>
              <Time value={made.created_at} />
              <br />
              <span className="muted">by {made.requested_by.name}</span>
            </td>
            <td>
              {made.selection}
              <br />
              <span className="muted">{made.reason}</span>
            </td>
            <td>
              <Badge muted={made.state !== "ready"}>{STATES[made.state]}</Badge>
              {made.state === "ready" ? (
                <>
                  {" "}
                  <a href={`/api/v1/bundles/${made.id}/content`} download={made.file_name}>
                    Download <span className="visually-hidden">{made.file_name}</span>
                  </a>
                  <br />
                  <span className="muted">
                    {holds(made)}
                    {made.size === null ? "" : ` · ${formatSize(made.size)}`}
                  </span>
                  <br />
                  <span className="muted">
                    SHA-256 <code title={made.sha256}>{made.sha256.slice(0, 12)}…</code>
                    {made.expires_at ? (
                      <>
                        {" "}
                        · deleted <RelativeTime value={made.expires_at} />
                      </>
                    ) : null}
                  </span>
                </>
              ) : null}
              {made.state === "failed" ? <p className="muted">{made.error}</p> : null}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
