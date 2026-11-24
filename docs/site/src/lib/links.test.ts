import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { type Options, rewrite } from "./links.ts";

const options: Options = {
  root: fileURLToPath(new URL("../../../../", import.meta.url)),
  sources: {
    "docs/adr/README.md": "/decisions/",
    "docs/adr/0003-roles-and-permissions.md": "/decisions/0003-roles-and-permissions/",
  },
  repository: "https://git.example/iki",
  ref: "main",
};

describe("rewrite", () => {
  it("leaves URLs and places in the page alone", () => {
    expect(rewrite("https://example.com/a.md", "README.md", options)).toBe(
      "https://example.com/a.md",
    );
    expect(rewrite("mailto:security@example.com", "README.md", options)).toBe(
      "mailto:security@example.com",
    );
    expect(rewrite("#decision", "README.md", options)).toBe("#decision");
  });

  it("sends a link to a file that is a page to that page, with its fragment", () => {
    expect(rewrite("0003-roles-and-permissions.md#decision", "docs/adr/0001-x.md", options)).toBe(
      "/decisions/0003-roles-and-permissions/#decision",
    );
  });

  it("reads a link that starts with / from the repository's root, as GitHub does", () => {
    expect(rewrite("/docs/adr/0003-roles-and-permissions.md", "docs/adr/0001-x.md", options)).toBe(
      "/decisions/0003-roles-and-permissions/",
    );
  });

  it("sends a link to a directory to its README's page", () => {
    expect(rewrite("adr/", "docs/roadmap.md", options)).toBe("/decisions/");
  });

  it("sends a link to any other file or directory to GitHub", () => {
    expect(rewrite("../../Makefile", "deploy/aws/README.md", options)).toBe(
      "https://git.example/iki/blob/main/Makefile",
    );
    expect(rewrite("modules/network/", "deploy/aws/README.md", options)).toBe(
      "https://git.example/iki/tree/main/deploy/aws/modules/network",
    );
  });

  it("sends a link past the repository's root to GitHub's own pages", () => {
    expect(rewrite("../../issues/new", "CONTRIBUTING.md", options)).toBe(
      "https://git.example/iki/issues/new",
    );
  });

  it("reads a site page's repo: link from the repository's root", () => {
    const page = "docs/site/src/content/use-cases/first-dose-response-assay.mdx";
    expect(rewrite("repo:cli/README.md#iki-starter", page, options)).toBe(
      "https://git.example/iki/blob/main/cli/README.md#iki-starter",
    );
    expect(rewrite("repo:docs/adr/README.md", page, options)).toBe("/decisions/");
  });

  it("leaves a link to nothing as written, for the site's check to report", () => {
    expect(rewrite("missing.md", "README.md", options)).toBe("missing.md");
  });
});
