// React Fast Refresh for `pnpm dev`, loaded before the app's entry module.
// Vite's backend-integration guide puts this in an inline script; Iki's CSP
// allows none, so it's a file the dev server serves. Never part of a build.
import RefreshRuntime from "/@react-refresh";

RefreshRuntime.injectIntoGlobalHook(window);
window.$RefreshReg$ = () => undefined;
window.$RefreshSig$ = () => (type) => type;
window.__vite_plugin_react_preamble_installed__ = true;
