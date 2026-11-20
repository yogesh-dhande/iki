// @ts-check
import js from "@eslint/js";
import prettier from "eslint-config-prettier";
import jsxA11y from "eslint-plugin-jsx-a11y";
import reactHooks from "eslint-plugin-react-hooks";
import tseslint from "typescript-eslint";

// The videos (media/videos): every colour, size and duration comes from the theme, so a
// video made by anyone, or any model, looks and moves like the others.
const videoThemeRules = [
  {
    selector: "Literal[value=/^#[0-9a-fA-F]{3,8}$/], Literal[value=/^(rgb|hsl)a?\\(/i]",
    message: "Colours come from the theme (media/videos/src/system/theme/theme.ts).",
  },
  {
    selector: "TemplateElement[value.raw=/#[0-9a-fA-F]{3,8}\\b|(rgb|hsl)a?\\(/i]",
    message: "Colours come from the theme (media/videos/src/system/theme/theme.ts).",
  },
  {
    selector:
      "JSXAttribute[name.name=/^(fontSize|fontWeight|lineHeight|letterSpacing|lineWidth|radius|padding|paddingTop|paddingBottom|paddingLeft|paddingRight|margin|gap|shadowBlur|shadowOffsetY|arrowSize|width|height|size|x|y)$/] > JSXExpressionContainer > Literal[raw=/^-?\\d/]",
    message: "Sizes come from the theme (media/videos/src/system/theme/theme.ts).",
  },
  {
    // A signal tweened over a literal number of seconds: node.opacity(1, 0.4).
    selector:
      "CallExpression[callee.property.name=/^(opacity|scale|position|x|y|rotation|start|end|size|width|height|fill|stroke|lineWidth|radius|absolutePosition|absoluteRotation|absoluteScale)$/] > Literal.arguments:nth-child(2)[raw=/^\\d/]",
    message: "Durations come from the theme (media/videos/src/system/theme/motion.ts).",
  },
  {
    selector:
      "CallExpression[callee.name=/^(waitFor|wait|delay)$/] > Literal.arguments:first-child[raw=/^\\d/]",
    message: "Durations come from the theme (media/videos/src/system/theme/motion.ts).",
  },
];

export default tseslint.config(
  {
    ignores: [
      "**/dist/**",
      "**/coverage/**",
      "**/node_modules/**",
      "worker/target/**",
      ".venv/**",
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
  {
    files: ["media/videos/src/**/*.{ts,tsx}"],
    ignores: ["media/videos/src/system/theme/**"],
    rules: { "no-restricted-syntax": ["error", ...videoThemeRules] },
  },
  {
    // A video's own folder holds no numbers at all, beyond 0 and 1: its script and the
    // theme do.
    files: ["media/videos/src/*/**/*.{ts,tsx}"],
    ignores: ["media/videos/src/system/**"],
    rules: {
      "no-restricted-syntax": [
        "error",
        ...videoThemeRules,
        {
          selector: "Literal[raw=/^(?![01]$)-?\\d/]",
          message:
            "Numbers belong in the script (src/<video>/script.yaml) or the theme, not a video's code.",
        },
      ],
    },
  },
  {
    // The videos' tools run in Node.
    files: ["media/videos/tools/**/*.ts", "media/videos/vite.config.ts"],
    languageOptions: { globals: { console: "readonly", process: "readonly" } },
  },
  prettier,
);
