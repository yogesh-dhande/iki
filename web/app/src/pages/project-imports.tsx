/**
 * Importing a bundle from another Iki deployment (ADR 0096): the Import card in a project's
 * settings uploads one, and lists the project's imports. Each import's page
 * (project-import.tsx) shows what Iki found when it checked it, and the plan for each entity
 * and person, before anything changes.
 */
import { isProblem, unwrap, type Schemas } from "@ikisci/sdk";
import { infiniteQueryOptions, queryOptions, useQuery } from "@tanstack/react-query";
import { Link, useNavigate } from "@tanstack/react-router";
import { useId, useRef, useState } from "react";

import { whileWorking } from "../activity";
import { iki, problemOf, queryClient, type Project } from "../api";
import { UploadStatus } from "../file-upload";
import { Badge, Button, Loading, ProblemAlert, Time, useMessages } from "../ui";
import { progressLabel, sendParts, sha256Of, uploadError, type Progress } from "../uploads";

export type Import = Schemas["ImportOut"];
export type Planned = Schemas["ImportEntityOut"];
export type Action = Planned["action"];

export const STATES: Record<Import["state"], string> = {
  scanning: "Scanning",
  quarantined: "Quarantined",
  not_scanned: "Not scanned",
  uploading: "Uploading",
  planning: "Checking…",
  planned: "Planned",
  refused: "Refused",
  applying: "Applying…",
  applied: "Applied",
  failed: "Failed",
  discarded: "Discarded",
};

export const IMPORTS = "imports";

function importsQuery(projectId: string) {
  return queryOptions({
    queryKey: [IMPORTS, "project", projectId],
    queryFn: async ({ signal }) =>
      unwrap(
        await iki.GET("/api/v1/projects/{project_id}/imports", {
          params: { path: { project_id: projectId } },
          signal,
        }),
      ).items,
    refetchInterval: (query) =>
      query.state.data?.some(
        (i) => i.state === "scanning" || i.state === "planning" || i.state === "applying",
      )
        ? 2000
        : false,
  });
}

export function importQuery(id: string) {
  return queryOptions({
    queryKey: [IMPORTS, id],
    queryFn: async ({ signal }) =>
      unwrap(
        await iki.GET("/api/v1/imports/{import_id}", {
          params: { path: { import_id: id } },
          signal,
        }),
      ),
    refetchInterval: (query) =>
      query.state.data?.state === "planning" ||
      query.state.data?.state === "uploading" ||
      query.state.data?.state === "scanning" ||
      query.state.data?.state === "applying"
        ? 2000
        : false,
  });
}

export function planResponse<T>(
  id: string,
  result: { data?: T; error?: unknown; response: Response },
): T {
  if (isProblem(result.error) && result.error.code === "stale_plan") {
    void queryClient.invalidateQueries({ queryKey: importQuery(id).queryKey, exact: true });
  }
  return unwrap(result);
}

// Applying a reviewed plan also fills in the generated HRIDs and placeholder accounts.
export function plannedQuery(made: Import, action: Action | undefined) {
  return infiniteQueryOptions({
    queryKey: [IMPORTS, made.id, made.planned_at, made.state, "entities", action ?? "all"],
    initialPageParam: undefined as string | undefined,
    queryFn: async ({ pageParam, signal }) =>
      planResponse(
        made.id,
        await iki.GET("/api/v1/imports/{import_id}/entities", {
          params: {
            path: { import_id: made.id },
            query: {
              limit: 100,
              ...(made.planned_at ? { planned_at: made.planned_at } : {}),
              ...(action ? { action } : {}),
              ...(pageParam ? { cursor: pageParam } : {}),
            },
          },
          signal,
        }),
      ),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
}

export function peopleQuery(made: Import) {
  return infiniteQueryOptions({
    queryKey: [IMPORTS, made.id, made.planned_at, made.state, "people"],
    initialPageParam: undefined as string | undefined,
    queryFn: async ({ pageParam, signal }) =>
      planResponse(
        made.id,
        await iki.GET("/api/v1/imports/{import_id}/people", {
          params: {
            path: { import_id: made.id },
            query: {
              limit: 100,
              ...(made.planned_at ? { planned_at: made.planned_at } : {}),
              ...(pageParam ? { cursor: pageParam } : {}),
            },
          },
          signal,
        }),
      ),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
}

/** Uploads a bundle to import into the project, as a file is uploaded (ADR 0036). Iki then
 * checks it and plans the import. The person waits on it, so they count as active while it
 * runs (ADR 0153). */
export function importBundle(
  file: File,
  projectId: string,
  onProgress: (progress: Progress) => void,
  signal?: AbortSignal,
): Promise<Import> {
  return whileWorking(() => sendBundle(file, projectId, onProgress, signal));
}

async function sendBundle(
  file: File,
  projectId: string,
  onProgress: (progress: Progress) => void,
  signal?: AbortSignal,
): Promise<Import> {
  onProgress({ phase: "hashing", done: 0, total: file.size });
  const hash = await sha256Of(
    file,
    (done) => {
      onProgress({ phase: "hashing", done, total: file.size });
    },
    signal,
  );
  const started = unwrap(
    await iki.POST("/api/v1/projects/{project_id}/imports", {
      params: { path: { project_id: projectId } },
      body: { name: file.name, size: file.size, sha256: hash },
    }),
  );
  const path = { import_id: started.id };
  try {
    await sendParts(
      file,
      started.parts,
      started.parts[0]?.size ?? file.size,
      async (numbers) =>
        unwrap(
          await iki.POST("/api/v1/imports/{import_id}/parts", {
            params: { path },
            body: { numbers },
          }),
        ),
      onProgress,
      signal,
    );
    onProgress({ phase: "checking" });
    return unwrap(await iki.POST("/api/v1/imports/{import_id}/complete", { params: { path } }));
  } catch (error) {
    if (signal?.aborted) {
      // Nothing half-sent is kept.
      void iki.DELETE("/api/v1/imports/{import_id}", { params: { path } });
    }
    throw error;
  }
}

// --- The card in settings -----------------------------------------------------------------

export function Imports({ project }: { project: Project }) {
  const { say } = useMessages();
  const navigate = useNavigate();
  const id = useId();
  const imports = useQuery(importsQuery(project.id));
  const [file, setFile] = useState<File>();
  const [progress, setProgress] = useState<Progress>();
  const [error, setError] = useState<string>();
  const transfer = useRef<AbortController>(undefined);

  async function send(chosen: File) {
    setFile(chosen);
    setError(undefined);
    const controller = new AbortController();
    transfer.current = controller;
    try {
      const made = await importBundle(chosen, project.id, setProgress, controller.signal);
      say(`Uploaded ${chosen.name}. Iki is checking it and planning the import.`);
      await navigate({
        to: "/projects/$slug/imports/$importId/",
        params: { slug: project.slug, importId: made.id },
      });
    } catch (caught) {
      if (!controller.signal.aborted) setError(uploadError(caught));
    } finally {
      setProgress(undefined);
      setFile(undefined);
    }
  }

  return (
    <section className={imports.data?.length ? "card card-wide" : "card"}>
      <h2 className="section-title">Import</h2>
      <p>
        Bring in a bundle exported from another Iki deployment that this one trusts, such as from
        staging. Iki checks it and shows what importing it would do before anything changes.
      </p>
      {file && progress ? (
        <div className="upload">
          <p className="visually-hidden" role="status">
            {progressLabel(progress, file.name)}
          </p>
          <p aria-hidden="true">
            <UploadStatus name={file.name} progress={progress} />
          </p>
          {progress.phase === "checking" || progress.phase === "scanning" ? (
            <progress aria-label={progressLabel(progress, file.name)} />
          ) : (
            <>
              <progress
                aria-label={`${progress.phase === "hashing" ? "Reading" : "Uploading"} ${file.name}`}
                max={progress.total || 1}
                value={progress.done}
              />
              <Button
                variant="quiet"
                label={`Cancel uploading ${file.name}`}
                onClick={() => {
                  transfer.current?.abort();
                }}
              >
                Cancel
              </Button>
            </>
          )}
        </div>
      ) : (
        <div className={error ? "field field-invalid" : "field"}>
          <label htmlFor={`${id}-bundle`}>Bundle</label>
          <input
            id={`${id}-bundle`}
            type="file"
            accept=".tar.gz,.tgz,application/gzip"
            aria-invalid={error ? "true" : undefined}
            aria-describedby={error ? `${id}-error` : `${id}-help`}
            onChange={(event) => {
              const chosen = event.target.files?.[0];
              event.target.value = "";
              if (chosen) void send(chosen);
            }}
          />
          {error ? (
            <p className="field-error" id={`${id}-error`}>
              {error}
            </p>
          ) : (
            <p className="field-help" id={`${id}-help`}>
              A .iki-bundle.tar.gz file, from another deployment’s Export card.
            </p>
          )}
        </div>
      )}
      <ImportList project={project} query={imports} />
    </section>
  );
}

function ImportList({
  project,
  query,
}: {
  project: Project;
  query: ReturnType<typeof useQuery<Import[]>>;
}) {
  if (query.isError) return <ProblemAlert problem={problemOf(query.error)} />;
  if (!query.data) return <Loading label="Finding its imports…" />;
  if (query.data.length === 0) return null;
  return (
    <table className="table">
      <caption className="visually-hidden">Imports, newest first</caption>
      <thead>
        <tr>
          <th scope="col">Uploaded</th>
          <th scope="col">From</th>
          <th scope="col">State</th>
        </tr>
      </thead>
      <tbody>
        {query.data.map((made) => (
          <tr key={made.id}>
            <td>
              <Link
                to="/projects/$slug/imports/$importId/"
                params={{ slug: project.slug, importId: made.id }}
              >
                {made.name}
              </Link>
              <br />
              <span className="muted">
                <Time value={made.created_at} /> by {made.requested_by.name}
              </span>
            </td>
            <td>
              {made.source ? (
                <>
                  {made.source.deployment}
                  <br />
                  <span className="muted">{made.source.selection}</span>
                </>
              ) : (
                <span className="muted">Not read yet</span>
              )}
            </td>
            <td>
              <Badge muted={made.state !== "planned"}>{STATES[made.state]}</Badge>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
