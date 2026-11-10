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
      "**/.static/**",
      // The web app's build output, and the client generated from the API.
      "server/src/iki_server/web/static/app/**",
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
    // Vite's dev-only Fast Refresh preamble runs in the browser as a module.
    files: ["web/app/dev-preamble.js"],
    languageOptions: { globals: { window: "readonly" } },
  },
  prettier,
);
