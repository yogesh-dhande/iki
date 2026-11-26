import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { type Options, rewrite } from "./links.ts";

const options: Options = {
  root: fileURLToPath(new URL("../../../../", import.meta.url)),
  sources: {
    "plugins/plate_assays/README.md": "/plugins/plate-assays/",
    "plugins/plate_assays/data/reader.md": "/plugins/plate-assays/reader/",
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
    expect(rewrite("#example-files", "README.md", options)).toBe("#example-files");
  });

  it("sends a link to a file that is a page to that page, with its fragment", () => {
    expect(rewrite("data/reader.md#example-files", "plugins/plate_assays/README.md", options)).toBe(
      "/plugins/plate-assays/reader/#example-files",
    );
  });

  it("reads a link that starts with / from the repository's root, as GitHub does", () => {
    expect(rewrite("/plugins/plate_assays/data/reader.md", "cli/README.md", options)).toBe(
      "/plugins/plate-assays/reader/",
    );
  });

  it("sends a link to a directory to its README's page", () => {
    expect(rewrite("plate_assays/", "plugins/README.md", options)).toBe("/plugins/plate-assays/");
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
    const page = "docs/site/src/content/use-cases/dose-response-plate-assay.mdx";
    expect(rewrite("repo:cli/README.md#iki-starter", page, options)).toBe(
      "https://git.example/iki/blob/main/cli/README.md#iki-starter",
    );
    expect(rewrite("repo:plugins/plate_assays/README.md", page, options)).toBe(
      "/plugins/plate-assays/",
    );
  });

  it("leaves a link to nothing as written, for the site's check to report", () => {
    expect(rewrite("missing.md", "README.md", options)).toBe("missing.md");
  });
});
