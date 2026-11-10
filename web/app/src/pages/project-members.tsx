import { ApiError, unwrap } from "@ikisci/sdk";
import { useQuery, useSuspenseQuery } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { useState, type SubmitEvent } from "react";

import {
  findPerson,
  grantableTeamsQuery,
  iki,
  meQuery,
  membersQuery,
  problemOf,
  rolesQuery,
  useApiMutation,
  type Member,
  type Project,
  type Role,
  type RoleName,
} from "../api";
import { required, useForm } from "../forms";
import { usePageTitle } from "../layout";
import {
  Badge,
  Button,
  ConfirmDialog,
  Loading,
  ProblemAlert,
  RadioField,
  SelectField,
  TextField,
  useMessages,
} from "../ui";
import { useJoinAsAdmin, useProject } from "./project-overview";

/** "a Viewer", "an Admin", as the server's messages say it. */
function withArticle(label: string): string {
  return /^[AEIOU]/.test(label) ? `an ${label}` : `a ${label}`;
}

function who(member: Member): string {
  return member.user ? member.user.name || member.user.email : (member.team?.name ?? "");
}

export function ProjectMembers() {
  const project = useProject();
  usePageTitle(`Members · ${project.name}`);
  const me = useSuspenseQuery(meQuery).data;
  const roles = useSuspenseQuery(rolesQuery).data;
  const members = useQuery(membersQuery(project.id));
  const canManage = project.can.manage_members;
  const canJoin = project.can.manage_members && project.my_role !== "admin";

  return (
    <>
      {members.isPending ? (
        <Loading label="Loading members…" />
      ) : members.isError ? (
        <ProblemAlert problem={problemOf(members.error)} />
      ) : (
        <ul className="rows" aria-label="Members">
          {members.data.map((member) => (
            <MemberRow
              key={member.id}
              member={member}
              project={project}
              roles={roles}
              isMe={member.user?.id === me.id}
              canManage={canManage}
            />
          ))}
          {members.data.length === 0 ? (
            <li className="row">
              <span className="muted">No members.</span>
            </li>
          ) : null}
        </ul>
      )}

      {canJoin ? <JoinPanel project={project} /> : null}

      {canManage ? (
        <div className="columns">
          <AddPerson project={project} roles={roles} />
          <AddTeam project={project} roles={roles} />
        </div>
      ) : (
        <details className="disclosure">
          <summary>What each role can do</summary>
          <dl className="summary">
            {roles.map((role) => (
              <RoleTerm key={role.name} role={role} />
            ))}
          </dl>
        </details>
      )}
    </>
  );
}

function RoleTerm({ role }: { role: Role }) {
  return (
    <>
      <dt>{role.label}</dt>
      <dd>{role.description}</dd>
    </>
  );
}

function JoinPanel({ project }: { project: Project }) {
  const { join, busy, problem } = useJoinAsAdmin(project);
  return (
    <div className="panel">
      <p>You can manage this project’s members because you’re an Org Admin.</p>
      <ProblemAlert problem={problem} />
      <Button variant="quiet" busy={busy} busyText="Adding…" onClick={join}>
        Add me as Admin
      </Button>
    </div>
  );
}

function grantPath(project: Project, member: Member) {
  return member.user
    ? iki.PUT("/api/v1/projects/{project_id}/members/users/{user_id}", {
        params: { path: { project_id: project.id, user_id: member.user.id } },
        body: { role: member.role },
      })
    : iki.PUT("/api/v1/projects/{project_id}/members/teams/{team_id}", {
        params: { path: { project_id: project.id, team_id: member.team?.id ?? "" } },
        body: { role: member.role },
      });
}

function MemberRow({
  member,
  project,
  roles,
  isMe,
  canManage,
}: {
  member: Member;
  project: Project;
  roles: Role[];
  isMe: boolean;
  canManage: boolean;
}) {
  const { say } = useMessages();
  const navigate = useNavigate();
  const [role, setRole] = useState<RoleName>(member.role);
  const [confirming, setConfirming] = useState(false);
  const label = (name: RoleName) => roles.find((r) => r.name === name)?.label ?? name;
  const name = who(member);
  const invalidate = [["projects"], ["me"]] as const;

  const change = useApiMutation(
    async () => unwrap(await grantPath(project, { ...member, role })),
    invalidate,
  );
  const remove = useApiMutation(async () => {
    unwrap(
      await iki.DELETE("/api/v1/projects/{project_id}/members/{membership_id}", {
        params: { path: { project_id: project.id, membership_id: member.id } },
      }),
    );
  }, invalidate);
  const problem = change.error ?? (confirming ? undefined : remove.error);

  return (
    <li className="row">
      <div className="row-main">
        {member.user ? (
          <>
            <span className="row-title">
              {name}
              {isMe ? <span className="muted"> (you)</span> : null}
              {member.user.kind === "service" ? (
                <>
                  {" "}
                  <Badge muted>Service account</Badge>
                </>
              ) : null}
            </span>
            <span className="row-meta">
              {member.user.email}
              {member.user.active ? "" : " · deactivated"}
            </span>
          </>
        ) : (
          <>
            <span className="row-title">
              {name} <Badge muted>Team</Badge>
            </span>
            <span className="row-meta">
              Everyone in the team · {member.team_size ?? 0} member
              {member.team_size === 1 ? "" : "s"}
            </span>
          </>
        )}
      </div>
      {canManage ? (
        <div className="row-actions">
          <SelectField
            compact
            label={`Role for ${name}`}
            value={role}
            onChange={setRole}
            options={roles.map((r) => ({ value: r.name, label: r.label }))}
          />
          <Button
            variant="quiet"
            busy={change.isPending}
            busyText="Changing…"
            disabled={role === member.role}
            onClick={() => {
              change.mutate(undefined, {
                onSuccess: () => {
                  say(`${name} is now ${withArticle(label(role))}.`);
                },
              });
            }}
          >
            Change
          </Button>
          <Button
            variant="danger"
            onClick={() => {
              remove.reset();
              setConfirming(true);
            }}
          >
            Remove
          </Button>
          <ConfirmDialog
            isOpen={confirming}
            title={`Remove ${name}?`}
            confirmLabel="Remove"
            busy={remove.isPending}
            problem={remove.error ? problemOf(remove.error) : undefined}
            onCancel={() => {
              setConfirming(false);
            }}
            onConfirm={() => {
              remove.mutate(undefined, {
                onSuccess: () => {
                  setConfirming(false);
                  if (isMe) {
                    void navigate({ to: "/projects/" }).then(() => {
                      say(`Removed ${name} from ${project.name}.`);
                    });
                    return;
                  }
                  say(`Removed ${name} from ${project.name}.`);
                },
              });
            }}
          >
            <p>
              {member.user
                ? isMe
                  ? `You’ll lose the access your ${label(member.role)} role gives you to ${project.name}.`
                  : `${name} will lose the access their ${label(member.role)} role gives them to ${project.name}.`
                : `Everyone in ${name} will lose the access this grant gives them, unless they have a role of their own.`}
            </p>
          </ConfirmDialog>
        </div>
      ) : (
        <div className="row-tags">
          <Badge>{label(member.role)}</Badge>
        </div>
      )}
      {problem ? (
        <div className="row-problem">
          <ProblemAlert problem={problemOf(problem)} />
        </div>
      ) : null}
    </li>
  );
}

const EMAIL = /^[^\s@]+@[^\s@]+$/;

function lookupFailed(detail: string): ApiError {
  return new ApiError({
    type: "about:blank",
    title: "Unprocessable Content",
    status: 422,
    code: "validation_failed",
    detail,
    errors: [{ pointer: "#/email", detail }],
  });
}

function roleOptions(roles: Role[]) {
  return roles.map((r) => ({ value: r.name, label: r.label, description: r.description }));
}

function AddPerson({ project, roles }: { project: Project; roles: Role[] }) {
  const { say } = useMessages();
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<RoleName>("viewer");
  const add = useApiMutation(async () => {
    const person = await findPerson(email);
    if (!person) {
      throw lookupFailed(
        "No one with that email address has signed in to Iki yet. Ask them to sign in once, then add them.",
      );
    }
    if (!person.active) {
      throw lookupFailed("That person’s account is deactivated.");
    }
    unwrap(
      await iki.PUT("/api/v1/projects/{project_id}/members/users/{user_id}", {
        params: { path: { project_id: project.id, user_id: person.id } },
        body: { role },
      }),
    );
    return person;
  }, [["projects"], ["me"]]);
  const form = useForm(add.error);

  function submit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    const trimmed = email.trim();
    if (
      !form.validate({
        email:
          required(trimmed, "Enter their email address.") ||
          (!EMAIL.test(trimmed) && "Enter an email address like name@example.com."),
      })
    ) {
      return;
    }
    add.mutate(undefined, {
      onSuccess: (person) => {
        const label = roles.find((r) => r.name === role)?.label ?? role;
        say(`${person.name || person.email} is now ${withArticle(label)}.`);
        setEmail("");
        setRole("viewer");
      },
    });
  }

  return (
    <section className="card">
      <h2 className="section-title">Add a person</h2>
      {form.alert}
      <form {...form.props} onSubmit={submit}>
        <TextField
          type="email"
          label="Email address"
          value={email}
          onChange={setEmail}
          error={form.errors["email"]}
          help="People can be added once they’ve signed in to Iki for the first time. Add a service account by its address."
          maxLength={254}
        />
        <RadioField label="Role" value={role} onChange={setRole} options={roleOptions(roles)} />
        <Button type="submit" variant="primary" busy={add.isPending} busyText="Adding…">
          Add person
        </Button>
      </form>
    </section>
  );
}

function AddTeam({ project, roles }: { project: Project; roles: Role[] }) {
  const { say } = useMessages();
  const teams = useQuery(grantableTeamsQuery(project.id)).data ?? [];
  const [chosen, setChosen] = useState("");
  const [role, setRole] = useState<RoleName>("viewer");
  const team = teams.find((t) => t.id === chosen) ?? teams[0];
  const add = useApiMutation(
    async () =>
      unwrap(
        await iki.PUT("/api/v1/projects/{project_id}/members/teams/{team_id}", {
          params: { path: { project_id: project.id, team_id: team?.id ?? "" } },
          body: { role },
        }),
      ),
    [["projects"], ["me"]],
  );
  const form = useForm(add.error);

  if (teams.length === 0) return null;

  function submit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!team) return;
    const name = team.name;
    add.mutate(undefined, {
      onSuccess: () => {
        const label = roles.find((r) => r.name === role)?.label ?? role;
        say(`Everyone in ${name} is now ${withArticle(label)}.`);
        setChosen("");
        setRole("viewer");
      },
    });
  }

  return (
    <section className="card">
      <h2 className="section-title">Add a team</h2>
      {form.alert}
      <form {...form.props} onSubmit={submit}>
        <SelectField
          label="Team"
          value={team?.id ?? ""}
          onChange={setChosen}
          options={teams.map((t) => ({ value: t.id, label: t.name }))}
        />
        <RadioField
          label="Role"
          value={role}
          onChange={setRole}
          options={roleOptions(roles)}
          help="Everyone in the team gets this role, including people who join it later."
        />
        <Button type="submit" variant="primary" busy={add.isPending} busyText="Adding…">
          Add team
        </Button>
      </form>
    </section>
  );
}
