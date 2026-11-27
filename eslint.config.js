// @ts-check
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
      // Built by plugins/small_molecule/web/build.js: RDKit.js, minified, and the
      // component's source, which is linted where it's written (web/src).
      "plugins/small_molecule/components/depiction.js",
      // The source adapters are linted before bundling for the component sandbox.
      "plugins/equipment/components/booking-calendar.js",
      "plugins/plasmid/components/plasmid-map.js",
      "plugins/oligo/components/chemistry.js",
      "plugins/oligo/components/duplex.js",
      "plugins/complex/components/composition.js",
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
    // The one place the header is spelled out.
    files: ["web/app/src/revisioned.ts"],
    rules: { "no-restricted-syntax": ["error", RAW_HTML, QUERY_SIGNAL, POST_READ] },
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
    // Plugins' components: Web Components that run in a sandboxed frame (ADR 0084).
    files: [
      "**/plugins/**/components/**/*.{js,mjs}",
      "plugins/*/web/src/**/*.js",
      "plugin-template/components/**/*.{js,mjs}",
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
