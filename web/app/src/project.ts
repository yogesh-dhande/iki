/** The project a page is about, and what its pages share about it. */

import { unwrap } from "@ikisci/sdk";
import { useSuspenseQuery } from "@tanstack/react-query";
import { useParams } from "@tanstack/react-router";

import { iki, meQuery, problemOf, projectQuery, useApiMutation, type Project } from "./api";
import { useMessages } from "./ui";

/** The project the current page is about, from the `$slug` in its address. */
export function useProject(): Project {
  const { slug } = useParams({ from: "/projects/$slug" });
  return useSuspenseQuery(projectQuery(slug)).data;
}

/** An Org Admin who isn't a project Admin makes themselves one. The server records it. */
export function useJoinAsAdmin(project: Project) {
  const me = useSuspenseQuery(meQuery).data;
  const { say } = useMessages();
  const join = useApiMutation(
    async () =>
      unwrap(
        await iki.PUT("/api/v1/projects/{project_id}/members/users/{user_id}", {
          params: { path: { project_id: project.id, user_id: me.id } },
          body: { role: "admin" },
        }),
      ),
    [["projects"], ["me"]],
  );
  return {
    join: () => {
      join.mutate(undefined, {
        onSuccess: () => {
          say(`You’re now an Admin of ${project.name}.`);
        },
      });
    },
    busy: join.isPending,
    problem: join.error ? problemOf(join.error) : undefined,
  };
}
