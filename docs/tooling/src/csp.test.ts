import { createHash } from "node:crypto";
import { mkdir, mkdtemp, readFile, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { addPolicies, withPolicy } from "./csp.ts";

const hash = (text: string) =>
  `'sha256-${createHash("sha256").update(text, "utf-8").digest("base64")}'`;

const page = (head: string, body = "") =>
  `<!doctype html><html><head><meta charset="utf-8">${head}</head><body>${body}</body></html>`;

function policyOf(html: string): string {
  const match = /<meta http-equiv="Content-Security-Policy" content="([^"]*)">/.exec(html);
  if (!match?.[1]) {
    throw new Error("No policy");
  }
  return match[1];
}

function directive(policy: string, name: string): string {
  const found = policy.split("; ").find((part) => part.startsWith(`${name} `));
  if (found === undefined) {
    throw new Error(`No ${name}`);
  }
  return found;
}

describe("withPolicy", () => {
  it("puts the policy right after the charset, before anything it governs", () => {
    const html = withPolicy(page("<script>go()</script>"));
    expect(html.indexOf("Content-Security-Policy")).toBeGreaterThan(html.indexOf("charset"));
    expect(html.indexOf("Content-Security-Policy")).toBeLessThan(html.indexOf("<script>"));
  });

  it("allows the page's own inline scripts and style elements, each by its hash", () => {
    const policy = policyOf(withPolicy(page("<script>go()</script><style>p{color:red}</style>")));
    expect(directive(policy, "script-src")).toBe(
      `script-src 'self' 'wasm-unsafe-eval' ${hash("go()")}`,
    );
    expect(directive(policy, "style-src")).toBe(`style-src 'self' ${hash("p{color:red}")}`);
    expect(policy.startsWith("default-src 'none'; ")).toBe(true);
  });

  it("names style attributes by hash only when a page has them", () => {
    const plain = policyOf(withPolicy(page("")));
    expect(directive(plain, "style-src")).toBe("style-src 'self'");
    const styled = policyOf(withPolicy(page("", '<p style="--depth: 0;">x</p>')));
    expect(directive(styled, "style-src")).toBe(
      `style-src 'self' 'unsafe-hashes' ${hash("--depth: 0;")}`,
    );
  });

  it("allows nothing else inline: data scripts are inert and external ones are the site's", () => {
    const policy = policyOf(
      withPolicy(page('<script type="application/json">{"a": 1}</script><script src="/a.js">')),
    );
    expect(directive(policy, "script-src")).toBe("script-src 'self' 'wasm-unsafe-eval'");
    expect(policy).not.toContain("unsafe-inline");
    expect(directive(policy, "frame-src")).toBe("frame-src 'self'");
  });

  it("lets a viewer's frame add styles as it draws, and frame nothing itself", () => {
    const policy = policyOf(withPolicy(page("<style>a{}</style>"), { frame: true }));
    expect(directive(policy, "style-src")).toBe("style-src 'self' 'unsafe-inline'");
    expect(directive(policy, "frame-src")).toBe("frame-src 'none'");
    expect(directive(policy, "script-src")).not.toContain("unsafe-inline");
  });

  it("lists each hash once, in order, so the same page gets the same policy", () => {
    const policy = policyOf(
      withPolicy(page("<script>b()</script><script>a()</script><script>a()</script>")),
    );
    expect(policy.split(hash("a()")).length).toBe(2);
    expect(policy.indexOf(hash("a()")) < policy.indexOf(hash("b()"))).toBe(
      hash("a()") < hash("b()"),
    );
  });

  it("refuses a page without a charset, or one that has a policy already", () => {
    expect(() => withPolicy("<html><head></head></html>")).toThrow(/charset/);
    expect(() => withPolicy(withPolicy(page("")))).toThrow(/already/);
  });
});

describe("addPolicies", () => {
  it("gives every page its policy, and the viewers' frames theirs", async () => {
    const dist = await mkdtemp(join(tmpdir(), "csp-"));
    await mkdir(join(dist, "guide"));
    await mkdir(join(dist, "viewers", "map"), { recursive: true });
    await writeFile(join(dist, "index.html"), page(""));
    await writeFile(join(dist, "guide", "index.html"), page(""));
    await writeFile(join(dist, "viewers", "map", "index.html"), page(""));
    await writeFile(join(dist, "style.css"), "p{}");

    expect(await addPolicies(dist)).toBe(3);

    const read = async (...path: string[]) =>
      policyOf(await readFile(join(dist, ...path), "utf-8"));
    expect(await read("guide", "index.html")).not.toContain("unsafe-inline");
    expect(await read("viewers", "map", "index.html")).toContain("'unsafe-inline'");
    expect(await readFile(join(dist, "style.css"), "utf-8")).toBe("p{}");
  });
});
