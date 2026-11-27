/**
 * Outside APIs (ADR 0130): models and services that actions send data to, outside Iki. An
 * Org Admin approves each for a project; before launching, people see what each sends,
 * and where, and agree to it. The launch forms show that here.
 */

import { unwrap, type Schemas } from "@ikisci/sdk";
import { queryOptions, useQuery } from "@tanstack/react-query";
import { useId } from "react";

import { everyPage, iki } from "./api";

export type OutsideApi = Schemas["OutsideApiOut"];
export type Statement = Schemas["ConsentStatementOut"];
export type ApiCall = Schemas["ApiCallOut"];

export const OUTSIDE_APIS = ["outside-apis"] as const;

/** Every installed outside API, with each project's approval: for Org Admins. */
export const outsideApisQuery = queryOptions({
  queryKey: [...OUTSIDE_APIS, "all"],
  queryFn: ({ signal }) =>
    everyPage(async (page) =>
      unwrap(await iki.GET("/api/v1/outside-apis", { params: { query: page }, signal })),
    ),
});

/** Every installed outside API, with whether it's approved for the project. */
export function projectOutsideApisQuery(projectId: string) {
  return queryOptions({
    queryKey: [...OUTSIDE_APIS, "project", projectId],
    queryFn: ({ signal }) =>
      everyPage(async (page) =>
        unwrap(
          await iki.GET("/api/v1/projects/{project_id}/outside-apis", {
            params: { path: { project_id: projectId }, query: page },
            signal,
          }),
        ),
      ),
  });
}

/** The statements whose API isn't approved for the project, for what it names now. */
export function unapproved(statements: Statement[], apis: OutsideApi[]): Statement[] {
  return statements.filter(
    (s) => !apis.find((a) => a.api === s.api)?.approvals.some((approval) => approval.current),
  );
}

/** Each API once, in the order the statements name them: what `consent` names. */
export function consentFor(statements: Statement[]): string[] {
  return [...new Set(statements.map((s) => s.api))];
}

/** "FoldCloud (api.foldcloud.example)", and the others, as a sentence. */
export function destinations(statements: Statement[]): string {
  const seen = new Map(statements.map((s) => [s.api, `${s.label} (${s.host})`]));
  const names = [...seen.values()];
  if (names.length < 2) return names.join("");
  return `${names.slice(0, -1).join(", ")} and ${names[names.length - 1] ?? ""}`;
}

/**
 * Which of a launch's outside APIs aren't approved for the project: while any aren't,
 * `blocked` is true, as the launch would be refused.
 */
export function useSending(
  projectId: string,
  statements: Statement[],
): { blocked: boolean; unapproved: Statement[] } {
  const apis = useQuery({ ...projectOutsideApisQuery(projectId), enabled: statements.length > 0 });
  const missing = statements.length && apis.data ? unapproved(statements, apis.data) : [];
  return { blocked: missing.length > 0, unapproved: missing };
}

/**
 * What a launch sends outside Iki, and where, with a box to agree to it. When an API
 * isn't approved for the project, it says so instead, and who approves it.
 */
export function SendsOutside({
  projectName,
  statements,
  missing,
  agreed,
  onAgree,
  error,
}: {
  projectName: string;
  statements: Statement[];
  /** The ones not approved for the project. */
  missing: Statement[];
  agreed: boolean;
  onAgree: (agreed: boolean) => void;
  error?: string | undefined;
}) {
  const id = useId();
  if (!statements.length) return null;
  return (
    <fieldset className={error ? "field field-invalid sends-outside" : "field sends-outside"}>
      <legend>It sends data outside Iki</legend>
      <ul className="sends-outside-statements">
        {statements.map((s) => (
          <li key={`${s.step ?? ""}:${s.api}`}>
            {s.step ? (
              <>
                Step <code>{s.step}</code>:{" "}
              </>
            ) : null}
            {s.statement}
          </li>
        ))}
      </ul>
      {missing.length ? (
        <div className="alert alert-error" role="alert">
          <p>
            {destinations(missing)} {missing.length > 1 ? "aren’t" : "isn’t"} approved for{" "}
            {projectName}, so this can’t start. An Org Admin approves outside APIs for each project,
            in Admin → Outside APIs.
          </p>
        </div>
      ) : (
        <>
          {error ? (
            <p className="field-error" id={`${id}-error`}>
              {error}
            </p>
          ) : null}
          <label className="choice" htmlFor={id}>
            <input
              type="checkbox"
              id={id}
              checked={agreed}
              aria-invalid={error ? "true" : undefined}
              aria-describedby={error ? `${id}-error` : undefined}
              onChange={(event) => {
                onAgree(event.target.checked);
              }}
            />
            <span className="choice-text">I agree to send this to {destinations(statements)}</span>
          </label>
          <p className="field-help">
            Your agreement is kept with the run, as is each call: the model and version that
            answered, and fingerprints of what was sent and answered.
          </p>
        </>
      )}
    </fieldset>
  );
}

const KIND: Record<ApiCall["kind"], string> = {
  call: "Call",
  poll: "Poll",
  cancel: "Cancel",
};

export function callKind(kind: ApiCall["kind"]): string {
  return KIND[kind];
}
