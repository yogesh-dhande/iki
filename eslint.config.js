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
      // Plugins the tests built.
      ".pytest_cache/**",
      "**/.static/**",
      // The web app's build output, and the client generated from the API.
      "server/src/iki_server/web/static/app/**",
      "server/src/iki_server/web/static/viewers/**",
      "web/packages/sdk/src/schema.ts",
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
    files: ["**/*.js"],
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
    files: ["**/plugins/**/components/**/*.{js,mjs}", "plugin-template/components/**/*.{js,mjs}"],
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
    // Vite's dev-only Fast Refresh preamble runs in the browser as a module.
    files: ["web/app/dev-preamble.js"],
    languageOptions: { globals: { window: "readonly" } },
  },
  prettier,
);
