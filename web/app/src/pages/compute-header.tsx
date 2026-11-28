import { Link } from "@tanstack/react-router";

import { PageTitle } from "../ui";

/** Compute's own header, on each of its pages: its name, what it is, and its pages. */
export function ComputeHeader() {
  const tab = { activeProps: { "aria-current": "page" as const } };
  return (
    <>
      <div className="admin-lede">
        <PageTitle>Compute</PageTitle>
        <p>Where runs execute, and how much each project may use</p>
      </div>
      <nav className="admin-tabs" aria-label="Compute">
        <Link to="/admin/compute/" activeOptions={{ exact: true }} {...tab}>
          Overview
        </Link>
        <Link to="/admin/compute/workers/" {...tab}>
          Workers
        </Link>
        <Link to="/admin/compute/slurm/" {...tab}>
          Slurm clusters
        </Link>
        <Link to="/admin/compute/quotas/" {...tab}>
          Quotas
        </Link>
        <Link to="/admin/compute/volumes/" {...tab}>
          Volumes
        </Link>
        <Link to="/admin/compute/images/" {...tab}>
          Images
        </Link>
      </nav>
    </>
  );
}
