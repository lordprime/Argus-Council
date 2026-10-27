import { QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { RouterProvider } from "react-router/dom";

import { router } from "@/app/router";
import { queryClient } from "@/lib/queries";
import { useSession } from "@/stores/session";
import { applyTheme, useUi } from "@/stores/ui";

import "@/styles/index.css";

applyTheme(useUi.getState().theme);
document.documentElement.dataset.density = useUi.getState().density;
window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => applyTheme(useUi.getState().theme));
void useSession.getState().bootstrap();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  </StrictMode>,
);
