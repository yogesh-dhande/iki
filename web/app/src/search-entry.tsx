/**
 * Search everything on the server's own pages (sign-in settings, people, the audit log
 * and the rest), which have no web app around them: the same palette, opened by ⌘K or
 * anything that dispatches `iki:search`, opening what's chosen as a page load.
 */

import { QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { queryClient } from "./api";
import { SearchPalette } from "./search-palette";

const root = document.createElement("div");
root.id = "iki-search";
document.body.append(root);

createRoot(root).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <SearchPalette
        navigate={(href) => {
          window.location.assign(href);
        }}
      />
    </QueryClientProvider>
  </StrictMode>,
);
