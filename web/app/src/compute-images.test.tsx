/**
 * Compute → Images (ADR 0209), against a fake API: an environment bound to a deployment's
 * image, one with none and what to add, pins no installed version uses, none at all, and a
 * refused read. Only Compute admins have it.
 */

import type { Schemas } from "@ikisci/sdk";
import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { compatibleRange, exactVersion, shortImage } from "./compute";
import { renderApp } from "./entity-fixtures";
import { answer, calls, ME, problem, signedIn } from "./test-server";

const COMPUTE_ADMIN = { ...ME.can, manage_compute: true };
const DIGEST = "3f1c9a7e5b2d4c6a8e0f1b3d5a7c9e1f2b4d6a8c0e2f4a6b8d0c2e4f6a8b0c2d";
const IMAGE = `registry.lab.example.org/licensed/crispresso2@sha256:${DIGEST}`;
const OLD_IMAGE = `registry.lab.example.org/licensed/crispresso2@sha256:${"9".repeat(64)}`;

function crispresso(
  overrides: Partial<Schemas["EnvironmentImageOut"]> = {},
): Schemas["EnvironmentImageOut"] {
  return {
    environment: "acme/editing.crispresso2",
    label: "CRISPResso2",
    plugin: "acme/editing",
    plugin_title: "Genome editing",
    installed_version: "1.4.2",
    binding: { plugin_version: ">=1.2,<2", image: IMAGE, pull_secret: "lab-registry" },
    ...overrides,
  };
}

function cellRanger(): Schemas["EnvironmentImageOut"] {
  return {
    environment: "acme/single_cell.cellranger",
    label: "Cell Ranger",
    plugin: "acme/single_cell",
    plugin_title: "Single-cell RNA-seq",
    installed_version: "0.3.1",
    binding: null,
  };
}

function stale(): Schemas["UnusedImageBindingOut"] {
  return {
    environment: "acme/editing.crispresso2",
    plugin_version: ">=1,<1.2",
    image: OLD_IMAGE,
    pull_secret: null,
    installed_version: "1.4.2",
    reason: "other_release",
  };
}

function onImages(bindings: Schemas["ImageBindingsOut"] | Response): void {
  signedIn({ can: COMPUTE_ADMIN });
  answer("GET /api/v1/compute/image-bindings", bindings);
}

async function card(name: RegExp) {
  return within(await screen.findByRole("region", { name }));
}

function line(region: Pick<typeof screen, "getByText">, term: string) {
  return region.getByText(term, { selector: "dt" }).nextElementSibling;
}

describe("how images read", () => {
  it("cuts a digest to 12 digits and keeps a reference without one", () => {
    expect(shortImage(IMAGE)).toBe(
      "registry.lab.example.org/licensed/crispresso2@sha256:3f1c9a7e5b2d…",
    );
    expect(shortImage("registry.lab.example.org/tools:1.0")).toBe(
      "registry.lab.example.org/tools:1.0",
    );
  });

  it("pins the installed release in the example, as compute.yaml's comparisons read it", () => {
    expect(exactVersion("1.4.2")).toBe("==1.4.2");
    expect(exactVersion("2.0")).toBe("==2.0.0");
    expect(exactVersion("0.4.0-rc.1")).toBe("==0.4.0");
  });

  it("offers a range across a qualified minor or major release", () => {
    expect(compatibleRange("0.3.1")).toBe(">=0.3,<0.4");
    expect(compatibleRange("1.4.2")).toBe(">=1.4,<2");
    expect(compatibleRange("2.1.0rc1")).toBe(">=2.1,<3");
  });
});

describe("Compute → Images", () => {
  it("shows a bound environment's image, copyable in full, its range and pull Secret", async () => {
    onImages({ environments: [crispresso()], unused: [] });
    const { user } = await renderApp("/admin/compute/images/");
    const bound = await card(/^CRISPResso2/);
    expect(line(bound, "Plugin")).toHaveTextContent(
      "Genome editing acme/editing, version 1.4.2 installed",
    );
    const image = line(bound, "Image");
    expect(image).toHaveTextContent("crispresso2@sha256:3f1c9a7e5b2d…Copy");
    expect(image).not.toHaveTextContent(DIGEST);
    expect(bound.getByTitle(IMAGE)).toBeVisible();
    expect(line(bound, "For versions")).toHaveTextContent(">=1.2,<2");
    expect(line(bound, "Pull Secret")).toHaveTextContent("lab-registry");
    expect(bound.queryByText("No image bound")).not.toBeInTheDocument();
    await user.click(bound.getByRole("button", { name: "Copy CRISPResso2’s image reference" }));
    expect(await navigator.clipboard.readText()).toBe(IMAGE);
    expect(bound.getByRole("button", { name: "Copied" })).toBeVisible();
    // The tab is Compute's, beside Volumes.
    const tabs = screen.getByRole("navigation", { name: "Compute" });
    expect(within(tabs).getByRole("link", { name: "Images" })).toHaveAttribute(
      "aria-current",
      "page",
    );
  });

  it("says when a bound image has no pull Secret", async () => {
    onImages({
      environments: [
        crispresso({
          binding: { plugin_version: ">=1.2,<2", image: IMAGE, pull_secret: null },
        }),
      ],
      unused: [],
    });
    await renderApp("/admin/compute/images/");
    const bound = await card(/^CRISPResso2/);
    expect(line(bound, "Pull Secret")).toHaveTextContent(
      "None: machines pull it with their own registry access",
    );
  });

  it("says an unbound environment's cloud runs wait for a binding, and what to add", async () => {
    onImages({ environments: [cellRanger()], unused: [] });
    const { user } = await renderApp("/admin/compute/images/");
    const unbound = await card(/^Cell Ranger/);
    expect(unbound.getByRole("heading", { name: "No image bound" })).toBeVisible();
    expect(
      unbound.getByText(/cloud runs, on container and VM machines, are unavailable/),
    ).toHaveTextContent(
      "Its cloud runs, on container and VM machines, are unavailable until a Compute admin " +
        "adds an EnvironmentImageBinding for it to the deployment’s compute.yaml that " +
        "matches version 0.3.1.",
    );
    expect(unbound.queryByText("Image", { selector: "dt" })).not.toBeInTheDocument();
    const example = [
      "apiVersion: iki/v1",
      "kind: EnvironmentImageBinding",
      "environment: acme/single_cell.cellranger",
      'plugin_version: "==0.3.1"',
      "image: <registry>/<repository>@sha256:<digest>",
      "pull_secret: <secret name>  # Optional: Kubernetes templates only",
    ].join("\n");
    expect(
      unbound.getByRole("region", { name: "Example binding for Cell Ranger" }).textContent,
    ).toBe(example);
    await user.click(
      unbound.getByRole("button", { name: "Copy the example binding for Cell Ranger" }),
    );
    expect(await navigator.clipboard.readText()).toBe(example);
    expect(unbound.getByText(/reuses the image across releases/)).toHaveTextContent(
      'A range such as ">=0.3,<0.4" reuses the image across releases, once the lab has ' +
        "qualified it for them.",
    );
  });

  it("lists bindings no installed version uses, each saying why", async () => {
    onImages({
      environments: [crispresso()],
      unused: [
        stale(),
        {
          ...stale(),
          environment: "acme/retired.tools",
          plugin_version: ">=2",
          pull_secret: "old-registry",
          installed_version: null,
          reason: "not_installed",
        },
      ],
    });
    const { user } = await renderApp("/admin/compute/images/");
    const unused = await card(/Unused bindings/);
    const rows = unused.getAllByRole("listitem");
    expect(rows).toHaveLength(2);
    const [old, gone] = rows as [HTMLElement, HTMLElement];
    expect(old).toHaveTextContent(
      "acme/editing.crispresso2 UnusedFor versions >=1,<1.2. Version 1.4.2 is installed, " +
        "outside this range.",
    );
    expect(old).toHaveTextContent(`crispresso2@sha256:${"9".repeat(12)}…`);
    expect(gone).toHaveTextContent(
      "acme/retired.tools UnusedFor versions >=2 · pull Secret old-registry. No installed " +
        "plugin has this environment.",
    );
    await user.click(
      within(old).getByRole("button", {
        name: "Copy the unused image reference for acme/editing.crispresso2 >=1,<1.2",
      }),
    );
    expect(await navigator.clipboard.readText()).toBe(OLD_IMAGE);
    // The bound environment is still shown, above.
    expect(await card(/^CRISPResso2/)).toBeTruthy();
  });

  it("says why an overlapping or unrequested binding applies to nothing", async () => {
    onImages({
      environments: [crispresso({ binding: null })],
      unused: [
        { ...stale(), plugin_version: ">=1.4", reason: "overlapping" },
        { ...stale(), environment: "acme/editing.bowtie2", reason: "not_requested" },
      ],
    });
    await renderApp("/admin/compute/images/");
    const unused = await card(/Unused bindings/);
    const [overlap, unrequested] = unused.getAllByRole("listitem");
    expect(overlap).toHaveTextContent(
      "Another binding also matches version 1.4.2, so neither applies.",
    );
    expect(unrequested).toHaveTextContent(
      "Its environment doesn’t ask this deployment for an image.",
    );
  });

  it("says when no installed plugin needs an image", async () => {
    onImages({ environments: [], unused: [] });
    await renderApp("/admin/compute/images/");
    expect(
      await screen.findByText(/No installed plugin needs an image from this deployment/),
    ).toBeVisible();
    expect(screen.queryByRole("region", { name: /Unused bindings/ })).not.toBeInTheDocument();
  });

  it("shows a refused read", async () => {
    onImages(problem(403, "forbidden", "You need the Compute admin role."));
    await renderApp("/admin/compute/images/");
    expect(await screen.findByRole("alert")).toHaveTextContent("You need the Compute admin role.");
    expect(screen.queryByText("Loading images…")).not.toBeInTheDocument();
  });

  it("isn't anyone else's", async () => {
    signedIn();
    await renderApp("/admin/compute/images/");
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Not found");
    expect(calls("GET", "/api/v1/compute/image-bindings")).toEqual([]);
    expect(screen.queryByRole("link", { name: "Images" })).not.toBeInTheDocument();
  });
});
