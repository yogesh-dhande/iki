/**
 * Files (ADR 0177): a project's Files tab, the Files list across projects, and a file's
 * page, with what uses it.
 */

import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { Link, useNavigate, useParams, useSearch } from "@tanstack/react-router";
import { Fragment } from "react";

import { problemOf } from "../api";
import { formatSize, typeLabel, typesQuery } from "../entities";
import { FileTable } from "../file-table";
import { UploadFiles } from "../file-upload";
import {
  acquiredText,
  downloadHref,
  fileQuery,
  fileRecordsQuery,
  fileRunsQuery,
  filterOf,
  readGroupColumns,
  searchOf,
  spanText,
  statedFacts,
  usedByText,
  type FileDetail,
  type UsingRun,
} from "../files";
import { usePageTitle } from "../layout";
import { useReadableProjects } from "../lists";
import { useMedia } from "../media";
import { useProject } from "./project-overview";
import { StatusBadge } from "./runs";
import {
  Button,
  buttonClass,
  CopyValue,
  Loading,
  PageHeader,
  ProblemAlert,
  RelativeTime,
  Time,
} from "../ui";

/** A project's files: those added to it, and those its records hold. */
export function ProjectFiles() {
  const project = useProject();
  usePageTitle(`Files · ${project.name}`);
  const search = useSearch({ from: "/projects/$slug/files" });
  const navigate = useNavigate({ from: "/projects/$slug/files" });
  return (
    <FileTable
      projectId={project.id}
      filter={filterOf(search)}
      onFilter={(filter) => {
        void navigate({ search: searchOf(filter), replace: true });
      }}
      caption={`Files in ${project.name}`}
      actions={
        project.can.edit ? <UploadFiles projectId={project.id} projectName={project.name} /> : null
      }
    />
  );
}

/** Every file its reader can read, across their projects. */
export function AllFiles() {
  usePageTitle("Files");
  const search = useSearch({ from: "/files" });
  const navigate = useNavigate({ from: "/files" });
  const { projects } = useReadableProjects();
  const chosen = projects.find((p) => p.slug === search.project);
  return (
    <>
      <PageHeader
        title="Files"
        description="Every file in the projects you belong to, and those their records hold. Files in projects you aren’t in never appear."
      />
      <FileTable
        projectId={chosen?.id}
        across={{
          projects,
          slug: search.project,
          onProject: (slug) => {
            void navigate({
              search: { ...searchOf(filterOf(search)), ...(slug ? { project: slug } : {}) },
            });
          },
        }}
        filter={filterOf(search)}
        onFilter={(filter) => {
          void navigate({
            search: { ...searchOf(filter), ...(search.project ? { project: search.project } : {}) },
            replace: true,
          });
        }}
        caption="Files"
      />
    </>
  );
}

const ROLES: Record<UsingRun["roles"][number], string> = {
  param: "Given as a param",
  input: "Held by an input",
  made: "Made it",
};

function Origin({ file }: { file: FileDetail }) {
  const { origin } = file;
  switch (origin.kind) {
    case "run":
      return origin.run ? (
        <>
          Made by{" "}
          <Link to="/runs/$ref/" params={{ ref: origin.run.hrid }}>
            {origin.run.label} {origin.run.hrid}
          </Link>
        </>
      ) : (
        <>Made by a run you can’t read</>
      );
    case "registration":
      return (
        <>
          Registered where it is: <code className="file-uri">{origin.uri}</code>
          {origin.version ? (
            <>
              {" "}
              at version <code>{origin.version}</code>
            </>
          ) : null}
        </>
      );
    case "import":
      return <>Imported from another deployment, in a bundle</>;
    default:
      return <>Uploaded</>;
  }
}

/** A file: what it is, when its data was acquired, where it came from, and what uses it. */
export function FilePage() {
  const { id } = useParams({ from: "/files/$id" });
  const media = useMedia();
  const found = useQuery(fileQuery(id));
  const file = found.data;
  usePageTitle(file ? file.name : "File");
  if (found.error) {
    return (
      <>
        <PageHeader crumbs={[<Link to="/files/">Files</Link>, "File"]} title="File" />
        <ProblemAlert problem={problemOf(found.error)} />
      </>
    );
  }
  if (!file) return <Loading label="Loading the file…" />;
  return (
    <>
      <PageHeader
        crumbs={[
          <Link to="/files/">Files</Link>,
          ...(file.project
            ? [
                <Link to="/projects/$slug/files/" params={{ slug: file.project.slug }}>
                  {file.project.name}
                </Link>,
              ]
            : []),
          file.name,
        ]}
        title={file.name}
        actions={
          <a className={buttonClass("primary")} href={downloadHref(file.id)} download={file.name}>
            Download
          </a>
        }
      />
      <dl className="summary file-facts">
        <dt>Format</dt>
        <dd>
          {media.named(file.media_type)} <span className="muted">{file.media_type}</span>
        </dd>
        <dt>Size</dt>
        <dd>
          {formatSize(file.size)}{" "}
          <span className="muted">({file.size.toLocaleString()} bytes)</span>
        </dd>
        <dt>SHA-256</dt>
        <dd>
          <CopyValue value={file.sha256} label="the SHA-256" />
        </dd>
        <dt>Acquired</dt>
        <dd>
          {file.acquired ? (
            <>
              {acquiredText(file.acquired.at)}{" "}
              <span className="muted">{file.acquired.source_label}</span>
            </>
          ) : (
            <span className="muted">
              Unknown: its format doesn’t record it, and it wasn’t given when it was added.
            </span>
          )}
        </dd>
        {statedFacts(file.facts).map((fact) => (
          <Fragment key={fact.key}>
            <dt>{fact.label}</dt>
            <dd>
              {fact.value} <span className="muted">from {fact.statedIn}</span>
            </dd>
          </Fragment>
        ))}
        <dt>Added</dt>
        <dd>
          <Time value={file.created_at} />
          {file.created_by ? <> by {file.created_by.name}</> : null}
          {file.agent ? <> through {file.agent.name}</> : null}
          {file.project ? <> to {file.project.name}</> : null}
        </dd>
        <dt>Came from</dt>
        <dd>
          <Origin file={file} />
        </dd>
        {file.samples.length ? (
          <>
            <dt>{file.samples.length === 1 ? "Sample" : "Samples"}</dt>
            <dd>
              <Samples file={file} />
            </dd>
          </>
        ) : null}
      </dl>
      {file.read_groups.length ? <ReadGroups file={file} /> : null}
      <UsedBy file={file} />
    </>
  );
}

/** A SAM or BAM file's read groups (ADR 0224): a column for each tag any of them gives. */
function ReadGroups({ file }: { file: FileDetail }) {
  const columns = readGroupColumns(file.read_groups);
  return (
    <section className="file-read-groups" aria-labelledby="read-groups">
      <h2 className="section-title" id="read-groups">
        Read groups <span className="muted">{file.read_groups.length.toLocaleString()}</span>
      </h2>
      <p className="muted">As the file’s header gives them, in its @RG lines.</p>
      {/* Many tags can be wider than the page: its box scrolls, by keyboard too. */}
      <div
        className="list-table-box"
        role="region"
        aria-label={`Read groups in ${file.name}`}
        // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- a scrollable table
        tabIndex={0}
      >
        <table className="list-table">
          <caption className="visually-hidden">Read groups in {file.name}</caption>
          <thead>
            <tr>
              {columns.map(([tag, label]) => (
                <th scope="col" key={tag} title={`@RG ${tag}`}>
                  {label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {file.read_groups.map((group) => (
              <tr key={group.ID}>
                {columns.map(([tag]) => (
                  <td
                    key={tag}
                    className={
                      tag === "ID" ? "grid-id" : group[tag] === undefined ? "muted" : undefined
                    }
                  >
                    {group[tag] ?? "—"}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function Samples({ file }: { file: FileDetail }) {
  const types = useQuery(typesQuery).data ?? [];
  return (
    <ul className="file-samples">
      {file.samples.map((s) => (
        <li key={`${s.id} ${s.via_run?.id ?? ""}`}>
          <Link to="/entities/$ref/" params={{ ref: s.id }}>
            {s.title !== s.hrid ? `${s.title} ` : ""}
            <span className="grid-id">{s.hrid}</span>
          </Link>{" "}
          <span className="muted">
            {typeLabel(types, s.type)}
            {s.via_run ? (
              <>
                , read with it by{" "}
                <Link to="/runs/$ref/" params={{ ref: s.via_run.hrid }}>
                  {s.via_run.hrid}
                </Link>
              </>
            ) : null}
          </span>
        </li>
      ))}
    </ul>
  );
}

function UsedBy({ file }: { file: FileDetail }) {
  const records = useInfiniteQuery(fileRecordsQuery(file.id));
  const runs = useInfiniteQuery(fileRunsQuery(file.id));
  const types = useQuery(typesQuery).data ?? [];
  const held = records.data?.pages.flatMap((p) => p.items) ?? [];
  const used = runs.data?.pages.flatMap((p) => p.items) ?? [];
  return (
    <section className="file-used-by" aria-labelledby="used-by">
      <h2 className="section-title" id="used-by">
        Used by <span className="muted">{usedByText(file.used_by)}</span>
      </h2>
      <h3 className="file-used-heading">Records</h3>
      {records.error ? <ProblemAlert problem={problemOf(records.error)} /> : null}
      {records.isPending ? <Loading label="Loading records…" /> : null}
      {records.isSuccess && held.length === 0 ? (
        <p className="muted">No record you can read holds it.</p>
      ) : null}
      {held.length ? (
        <div className="list-table-box">
          <table className="list-table">
            <caption className="visually-hidden">Records that hold {file.name}</caption>
            <thead>
              <tr>
                <th scope="col" className="list-fill">
                  Record
                </th>
                <th scope="col">Type</th>
                <th scope="col">Revisions that held it</th>
                <th scope="col">Field</th>
              </tr>
            </thead>
            <tbody>
              {held.map((r) => {
                const fields = [...new Set(r.spans.flatMap((s) => s.fields))];
                return (
                  <tr key={r.id}>
                    <td className="list-fill">
                      <span className="list-fill-text">
                        <Link className="list-name" to="/entities/$ref/" params={{ ref: r.id }}>
                          {r.title}
                        </Link>
                        {r.title !== r.hrid ? (
                          <span className="cell-meta grid-id">{r.hrid}</span>
                        ) : null}
                      </span>
                    </td>
                    <td className="muted">{typeLabel(types, r.type)}</td>
                    <td>
                      {r.spans.map((s, i) => (
                        <span key={s.first}>
                          {i ? ", " : ""}
                          <Link
                            to="/entities/$ref/audit/"
                            params={{ ref: r.id }}
                            search={s.fields[0] ? { field: s.fields[0] } : {}}
                            aria-label={`${r.hrid} revisions ${spanText(s)}`}
                          >
                            {spanText(s)}
                          </Link>
                        </span>
                      ))}
                      <span className="cell-meta muted">of r{r.revision}</span>
                    </td>
                    <td className="muted">{fields.join(", ")}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : null}
      {records.hasNextPage ? (
        <div className="actions">
          <Button
            busy={records.isFetchingNextPage}
            busyText="Loading…"
            onClick={() => void records.fetchNextPage()}
          >
            Show more records
          </Button>
        </div>
      ) : null}
      <h3 className="file-used-heading">Runs</h3>
      {runs.error ? <ProblemAlert problem={problemOf(runs.error)} /> : null}
      {runs.isPending ? <Loading label="Loading runs…" /> : null}
      {runs.isSuccess && used.length === 0 ? (
        <p className="muted">No run you can read read or made it.</p>
      ) : null}
      {used.length ? (
        <div className="list-table-box">
          <table className="list-table">
            <caption className="visually-hidden">Runs that read or made {file.name}</caption>
            <thead>
              <tr>
                <th scope="col">Run</th>
                <th scope="col" className="list-fill">
                  Action
                </th>
                <th scope="col">How</th>
                <th scope="col">Status</th>
                <th scope="col">Started</th>
              </tr>
            </thead>
            <tbody>
              {used.map((run) => (
                <tr key={run.id}>
                  <td>
                    <Link className="grid-id" to="/runs/$ref/" params={{ ref: run.hrid }}>
                      {run.hrid}
                    </Link>
                  </td>
                  <td className="list-fill">
                    <span className="list-fill-text">
                      {run.label}
                      <span className="cell-meta muted">{run.project.name}</span>
                    </span>
                  </td>
                  <td className="muted">{run.roles.map((r) => ROLES[r]).join(", ")}</td>
                  <td>
                    <StatusBadge status={run.status} />
                  </td>
                  <td className="muted">
                    <RelativeTime value={run.created_at} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      {runs.hasNextPage ? (
        <div className="actions">
          <Button
            busy={runs.isFetchingNextPage}
            busyText="Loading…"
            onClick={() => void runs.fetchNextPage()}
          >
            Show more runs
          </Button>
        </div>
      ) : null}
    </section>
  );
}
