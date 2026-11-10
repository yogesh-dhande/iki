import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, beforeEach } from "vitest";

import { queryClient, resetSignedOut } from "./api";
import { resetApi, serverFetch } from "./test-server";

// The app's API client calls whatever `fetch` is at the time: here, a fake API.
globalThis.fetch = serverFetch;
// jsdom doesn't scroll; the router restores scroll positions.
window.scrollTo = () => undefined;

beforeEach(() => {
  document.head.innerHTML =
    '<meta name="csrf-token" content="test-csrf-token">' +
    '<meta name="iki-organization" content="Example Bio">';
});

afterEach(() => {
  cleanup();
  queryClient.clear();
  resetApi();
  resetSignedOut();
});
