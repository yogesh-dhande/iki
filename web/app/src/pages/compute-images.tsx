/**
 * Compute → Images (ADR 0209): the environments whose cloud runs need an image this
 * deployment supplies, such as a licensed tool's that Iki can't ship, and the pin each one's
 * installed plugin release matches in compute.yaml. Pins that no installed release matches
 * are listed too, so stale ones show. The pins change only in compute.yaml: this page reads.
 */

import { useQuery, useSuspenseQuery } from "@tanstack/react-query";

import { meQuery, problemOf } from "../api";
import {
  compatibleRange,
  exampleBinding,
  imageBindingsQuery,
  shortImage,
  type EnvironmentImage,
  type ImageBindings,
  type UnusedImageBinding,
} from "../compute";
import { usePageTitle } from "../layout";
import { CopyButton, CopyValue, Loading, ProblemAlert } from "../ui";
import { ComputeHeader } from "./compute";
import { NotFound } from "./errors";

export function ComputeImages() {
  usePageTitle("Images");
  const me = useSuspenseQuery(meQuery).data;
  const bindings = useQuery({ ...imageBindingsQuery, enabled: me.can.manage_compute });
  if (!me.can.manage_compute) return <NotFound />;
  return (
    <>
      <ComputeHeader />
      <p className="muted">
        Some plugins run tools whose licences let a lab use them but not Iki ship them, such as
        CRISPResso2 or Cell Ranger. Their environments’ cloud runs use an image from your own
        registry, pinned by digest in this deployment’s <code>compute.yaml</code>, which is reviewed
        like code: change the pins there, and they apply when the deployment is built again.
      </p>
      {bindings.isPending ? (
        <Loading label="Loading images…" />
      ) : bindings.isError ? (
        <ProblemAlert problem={problemOf(bindings.error)} />
      ) : (
        <Images bindings={bindings.data} />
      )}
    </>
  );
}

function Images({ bindings }: { bindings: ImageBindings }) {
  return (
    <>
      {bindings.environments.length === 0 ? (
        <section className="card">
          <p className="muted">
            No installed plugin needs an image from this deployment. A plugin that runs a tool Iki
            can’t ship asks for one, and shows here once it’s installed.
          </p>
        </section>
      ) : (
        bindings.environments.map((environment) => (
          <EnvironmentCard key={environment.environment} environment={environment} />
        ))
      )}
      {bindings.unused.length ? <Unused bindings={bindings.unused} /> : null}
    </>
  );
}

function EnvironmentCard({ environment }: { environment: EnvironmentImage }) {
  const titleId = `image-${environment.environment}`;
  const binding = environment.binding;
  return (
    <section className="card" aria-labelledby={titleId}>
      <h2 className="section-title" id={titleId}>
        {environment.label} <code className="muted">{environment.environment}</code>
      </h2>
      <dl className="summary">
        <dt>Plugin</dt>
        <dd>
          {environment.plugin_title} <code>{environment.plugin}</code>, version{" "}
          {environment.installed_version} installed
        </dd>
        {binding ? (
          <>
            <dt>Image</dt>
            <dd>
              <CopyValue
                value={binding.image}
                shown={shortImage(binding.image)}
                label={`${environment.label}’s image reference`}
              />
            </dd>
            <dt>For versions</dt>
            <dd>
              <code>{binding.plugin_version}</code>
            </dd>
            <dt>Pull Secret</dt>
            <dd>
              {binding.pull_secret ? (
                <code>{binding.pull_secret}</code>
              ) : (
                <span className="muted">None: machines pull it with their own registry access</span>
              )}
            </dd>
          </>
        ) : null}
      </dl>
      {binding ? null : <Unbound environment={environment} />}
    </section>
  );
}

function Unbound({ environment }: { environment: EnvironmentImage }) {
  const example = exampleBinding(environment);
  return (
    <div className="alert alert-warning">
      <h3 className="alert-title">No image bound</h3>
      <p>
        Its cloud runs, on container and VM machines, are unavailable until a Compute admin adds an{" "}
        <code>EnvironmentImageBinding</code> for it to the deployment’s <code>compute.yaml</code>{" "}
        that matches version {environment.installed_version}.
      </p>
      <div className="image-example-head">
        <p>For example:</p>
        <CopyButton value={example} label={`the example binding for ${environment.label}`} />
      </div>
      {/* Focusable, so a keyboard can scroll a long line (axe: scrollable-region-focusable). */}
      <pre
        className="code image-example"
        // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- it scrolls
        tabIndex={0}
        role="region"
        aria-label={`Example binding for ${environment.label}`}
      >
        <code>{example}</code>
      </pre>
      <p className="muted image-example-note">
        A range such as <code>&quot;{compatibleRange(environment.installed_version)}&quot;</code>{" "}
        reuses the image across releases, once the lab has qualified it for them.
      </p>
    </div>
  );
}

const WHY_UNUSED: Record<UnusedImageBinding["reason"], (binding: UnusedImageBinding) => string> = {
  other_release: (b) => `Version ${b.installed_version ?? ""} is installed, outside this range.`,
  not_installed: () => "No installed plugin has this environment.",
  not_requested: () => "Its environment doesn’t ask this deployment for an image.",
  overlapping: (b) =>
    `Another binding also matches version ${b.installed_version ?? ""}, so neither applies.`,
};

function Unused({ bindings }: { bindings: UnusedImageBinding[] }) {
  return (
    <section className="card" aria-labelledby="unused-images">
      <h2 className="section-title" id="unused-images">
        Unused bindings
      </h2>
      <p className="muted">
        In <code>compute.yaml</code>, but no installed plugin version matches them, so no new run
        uses them. One may be left from an earlier release, or ready for the next.
      </p>
      <ul className="rows">
        {bindings.map((binding) => (
          <li
            className="row"
            key={`${binding.environment} ${binding.plugin_version} ${binding.image}`}
          >
            <div className="row-main">
              <span className="row-title">
                <code>{binding.environment}</code> <span className="badge badge-muted">Unused</span>
              </span>
              <span className="row-meta">
                For versions <code>{binding.plugin_version}</code>
                {binding.pull_secret ? (
                  <>
                    {" · pull Secret "}
                    <code>{binding.pull_secret}</code>
                  </>
                ) : null}
                . {WHY_UNUSED[binding.reason](binding)}
              </span>
              <CopyValue
                value={binding.image}
                shown={shortImage(binding.image)}
                label={`the unused image reference for ${binding.environment} ${binding.plugin_version}`}
              />
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}
