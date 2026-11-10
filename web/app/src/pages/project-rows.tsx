import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";

import { rolesQuery, type Project, type RoleName } from "../api";
import { Badge } from "../ui";

export function useRoleLabel(): (role: RoleName) => string {
  const roles = useQuery(rolesQuery).data ?? [];
  return (role) => roles.find((r) => r.name === role)?.label ?? role;
}

function truncate(text: string, length: number): string {
  return text.length > length ? `${text.slice(0, length - 1)}…` : text;
}

export function ProjectRows({ projects }: { projects: Project[] }) {
  const roleLabel = useRoleLabel();
  return (
    <ul className="rows">
      {projects.map((project) => (
        <li className="row" key={project.id}>
          <div className="row-main">
            <Link className="row-title" to="/projects/$slug/" params={{ slug: project.slug }}>
              {project.name}
            </Link>
            <span className="row-meta">
              {project.team.name}
              {project.description ? ` · ${truncate(project.description, 90)}` : ""}
            </span>
          </div>
          <div className="row-tags">
            {project.archived ? <Badge muted>Archived</Badge> : null}
            {project.my_role ? (
              <Badge>{roleLabel(project.my_role)}</Badge>
            ) : (
              <Badge muted>Not a member</Badge>
            )}
          </div>
        </li>
      ))}
    </ul>
  );
}

/** Archived last, then by team and name, as the server-rendered list was. */
export function sortProjects(projects: Project[]): Project[] {
  return [...projects].sort(
    (a, b) =>
      Number(a.archived) - Number(b.archived) ||
      a.team.name.localeCompare(b.team.name) ||
      a.name.localeCompare(b.name),
  );
}
