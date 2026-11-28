// @ts-check
import { readFileSync } from "node:fs";
import { join } from "node:path";

import js from "@eslint/js";
import prettier from "eslint-config-prettier";
import jsxA11y from "eslint-plugin-jsx-a11y";
import reactHooks from "eslint-plugin-react-hooks";
import tseslint from "typescript-eslint";

// Raw HTML would bypass React's escaping, and Trusted Types refuse it anyway.
const RAW_HTML = {
  selector: "JSXAttribute[name.name='dangerouslySetInnerHTML']",
  message: "Render text, not HTML: the CSP's Trusted Types refuse raw HTML.",
};

// TanStack cancels a query (unmount, a new key) through its signal. page-reads.ts adds the
// document's lifetime to every page read, but only the query can pass its own signal.
const QUERY_SIGNAL = {
  selector: "Property[key.name='queryFn'] > :function:not(:has(Identifier[name='signal']))",
  message: "Pass the query's signal to its reads, so TanStack can cancel them.",
};

// A read that sends JSON (a search, a preview) is a page read by its meaning, not its method:
// it selects fetchPageRead, which stops it with the page. A write never does, so it finishes.
const POST_READ = {
  selector:
    "Property[key.name='queryFn'] CallExpression[callee.object.name='iki'][callee.property.name=/^(POST|PUT|PATCH|DELETE)$/]:not(:has(Property[key.name='fetch'][value.name='fetchPageRead']))",
  message: "A query's read that sends JSON selects fetchPageRead, so it stops with the page.",
};

// A change is made from the revision the person read (ADR 0035), through useRevisioned, which
// keeps it as it was when the dialog or form opened, or the SDK's ifMatch for a request that
// isn't one. A hand-built header is how a dialog came to send whatever a refetch brought in.
const RAW_IF_MATCH = {
  selector: "Literal[value=/^If-(None-)?Match$/i]",
  message:
    "Make the change from the revision read: useRevisioned in a dialog or form, else ifMatch.",
};
// The web app's pages: the modules its router fetches when one is first opened. Nothing imports
// one, the router included: a page imported from another module loads with that module, and
// with the first page if the app starts with it. What pages share goes in a module of its own.
// Importing only a page's types is fine. (`make web` weighs what the app starts with.)
const ROUTER = "web/app/src/router.tsx";
const PAGES = [
  ...readFileSync(join(import.meta.dirname, ROUTER), "utf8").matchAll(
    /import\("\.\/pages\/([\w-]+)"\)/g,
  ),
].map(([, page]) => page);
if (!PAGES.length) throw new Error(`No pages found in ${ROUTER}`);
/** @param {string} prefix how a module reaches the pages: `./`, `./pages/` or `../pages/` */
const PAGE_IMPORTS = (prefix) => ({
  patterns: [
    {
      regex: `^${prefix.replaceAll(".", "\\.")}(${PAGES.join("|")})$`,
      allowTypeImports: true,
      message:
        "A page loads when it's first opened, so nothing imports one: move what's shared into a module of its own.",
    },
  ],
});

export default tseslint.config(
  {
    ignores: [
      "**/dist/**",
      "**/coverage/**",
      // Binaryen's output; its Rust source and typed adapter are checked separately.
      "web/packages/science/generated/oligo-temperature.js",
      "**/node_modules/**",
      "worker/target/**",
      ".venv/**",
      ".venv-py312/**",
      // Claude Code's git worktrees: other checkouts, each checked on its own.
      ".claude/**",
      // The dev deployment's build (Tilt): copies of the plugins, linted where they live.
      "build/**",
      // Plugins the tests built.
      ".pytest_cache/**",
      "**/.static/**",
      // The web app's build output, and the client generated from the API.
      "server/src/iki_server/web/static/app/**",
      "server/src/iki_server/web/static/viewers/**",
      "web/packages/sdk/src/schema.ts",
      // Plugins' component modules are built from their web/src by iki-component-bundle
      // (small_molecule's by its own build.js, with RDKit.js): linted where they're written.
      "plugins/*/components/*.js",
      "examples/plugins/*/components/*.js",
    ],
  },
  js.configs.recommended,
  ...tseslint.configs.strictTypeChecked,
  ...tseslint.configs.stylisticTypeChecked,
  {
    languageOptions: {
      parserOptions: {
        projectService: { allowDefaultProject: ["*.js"] },
        tsconfigRootDir: import.meta.dirname,
      },
    },
  },
  {
    files: ["**/*.{js,mjs}"],
    ...tseslint.configs.disableTypeChecked,
  },
  {
    // Django's static scripts run in the browser as plain scripts, without a bundler.
    files: ["server/src/**/static/**/*.js"],
    languageOptions: {
      sourceType: "script",
      globals: {
        document: "readonly",
        history: "readonly",
        navigator: "readonly",
        setTimeout: "readonly",
        window: "readonly",
      },
    },
  },
  {
    // The web app (React): hooks used correctly, and accessible markup.
    files: ["web/app/**/*.{ts,tsx}"],
    ...reactHooks.configs.flat["recommended-latest"],
  },
  {
    files: ["web/app/**/*.{ts,tsx}"],
    ...jsxA11y.flatConfigs.strict,
    rules: {
      ...jsxA11y.flatConfigs.strict.rules,
      "no-restricted-syntax": ["error", RAW_HTML],
    },
  },
  {
    files: ["web/app/**/*.{ts,tsx}"],
    ignores: ["**/*.test.{ts,tsx}"],
    rules: { "no-restricted-syntax": ["error", RAW_HTML, QUERY_SIGNAL, POST_READ, RAW_IF_MATCH] },
  },
  {
    // The tests' fake API reads the header a change was sent with; it never sends one.
    files: ["web/app/src/test-server.ts"],
    rules: { "no-restricted-syntax": ["error", RAW_HTML, QUERY_SIGNAL, POST_READ] },
  },
  {
    files: ["web/app/src/pages/*.{ts,tsx}"],
    ignores: ["**/*.test.{ts,tsx}"],
    rules: { "@typescript-eslint/no-restricted-imports": ["error", PAGE_IMPORTS("./")] },
  },
  {
    files: ["web/app/src/*.{ts,tsx}"],
    ignores: ["**/*.test.{ts,tsx}"],
    rules: { "@typescript-eslint/no-restricted-imports": ["error", PAGE_IMPORTS("./pages/")] },
  },
  {
    files: ["web/app/src/*/**/*.{ts,tsx}"],
    ignores: ["**/*.test.{ts,tsx}", "web/app/src/pages/*"],
    rules: { "@typescript-eslint/no-restricted-imports": ["error", PAGE_IMPORTS("../pages/")] },
  },
  {
    // Iki's bridge in plugins' components' frames: a module, in the browser (ADR 0084).
    files: ["server/src/iki_server/components/bridge.js"],
    languageOptions: {
      globals: {
        ResizeObserver: "readonly",
        customElements: "readonly",
        document: "readonly",
        window: "readonly",
      },
    },
  },
  {
    // What every sandboxed frame runs first: a classic script, in the browser (ADR 0102).
    files: ["server/src/iki_server/web/frame_harden.js"],
    languageOptions: {
      sourceType: "script",
      globals: {
        DOMException: "readonly",
        Document: "readonly",
        Element: "readonly",
        MutationObserver: "readonly",
        ShadowRoot: "readonly",
        document: "readonly",
        location: "readonly",
        setInterval: "readonly",
        window: "readonly",
      },
    },
  },
  {
    // Plugins' components: Web Components that run in a sandboxed frame (ADR 0084). The
    // spec tests' plugins write theirs as they run, unbuilt.
    files: [
      "plugins/*/web/src/**/*.js",
      "examples/plugins/*/web/src/**/*.js",
      "spec/tests/plugins/*/components/*.js",
    ],
    languageOptions: {
      globals: {
        CustomEvent: "readonly",
        HTMLElement: "readonly",
        customElements: "readonly",
        document: "readonly",
      },
    },
  },
  {
    // The small_molecule plugin's component builder, run by Node.
    files: ["plugins/small_molecule/web/*.js"],
    languageOptions: {
      globals: { Buffer: "readonly", console: "readonly", process: "readonly" },
    },
  },
  {
    // The small_molecule plugin's depiction, before it's built: a Web Component in a
    // sandboxed frame (ADR 0084), that unpacks and runs RDKit's WebAssembly.
    files: ["plugins/small_molecule/web/src/**/*.js"],
    languageOptions: {
      globals: {
        Blob: "readonly",
        DecompressionStream: "readonly",
        HTMLElement: "readonly",
        Response: "readonly",
        URL: "readonly",
        WebAssembly: "readonly",
        atob: "readonly",
        customElements: "readonly",
        document: "readonly",
      },
    },
  },
  {
    // Vite's dev-only Fast Refresh preamble runs in the browser as a module.
    files: ["web/app/dev-preamble.js"],
    languageOptions: { globals: { window: "readonly" } },
  },
  prettier,
);
