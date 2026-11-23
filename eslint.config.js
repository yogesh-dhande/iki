// @ts-check
import js from "@eslint/js";
import prettier from "eslint-config-prettier";
import jsxA11y from "eslint-plugin-jsx-a11y";
import reactHooks from "eslint-plugin-react-hooks";
import tseslint from "typescript-eslint";

export default tseslint.config(
  {
    ignores: [
      "**/dist/**",
      "**/coverage/**",
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
      "plugins/example_lims/components/booking-calendar.js",
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
      // Raw HTML would bypass React's escaping, and Trusted Types refuse it anyway.
      "no-restricted-syntax": [
        "error",
        {
          selector: "JSXAttribute[name.name='dangerouslySetInnerHTML']",
          message: "Render text, not HTML: the CSP's Trusted Types refuse raw HTML.",
        },
      ],
    },
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
