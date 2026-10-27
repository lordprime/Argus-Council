import { lazy, Suspense, type ComponentType } from "react";
import { createBrowserRouter, Navigate, Outlet, useLocation } from "react-router";

import { AppShell } from "@/components/layout/AppShell";
import { Skeleton } from "@/components/ui";
import { LoginPage, RegisterPage } from "@/routes/Auth";
import { useSession } from "@/stores/session";

function page(load: () => Promise<{ default: ComponentType }>) {
  const C = lazy(load);
  return (
    <Suspense fallback={<div className="p-6 space-y-3"><Skeleton className="h-8 w-64" /><Skeleton className="h-40" /></div>}>
      <C />
    </Suspense>
  );
}

function RequireAuth() {
  const { status } = useSession();
  const loc = useLocation();
  if (status === "unknown") return <div className="p-6"><Skeleton className="h-10 w-full" /></div>;
  if (status === "anonymous") return <Navigate to={`/login?next=${encodeURIComponent(loc.pathname + loc.search)}`} replace />;
  return (
    <AppShell>
      <Outlet />
    </AppShell>
  );
}

export const router = createBrowserRouter([
  { path: "/login", element: <LoginPage /> },
  { path: "/register", element: <RegisterPage /> },
  {
    element: <RequireAuth />,
    children: [
      { index: true, element: page(() => import("@/routes/Home")) },
      { path: "runs", element: page(() => import("@/routes/RunsList")) },
      { path: "runs/:runId/*", element: page(() => import("@/routes/RunWorkspace")) },
      { path: "councils", element: page(() => import("@/routes/councils/CouncilsList")) },
      { path: "councils/new", element: page(() => import("@/routes/councils/CouncilBuilder")) },
      { path: "councils/:councilId", element: page(() => import("@/routes/councils/CouncilDetail")) },
      { path: "skills", element: page(() => import("@/routes/skills/SkillsList")) },
      { path: "skills/new", element: page(() => import("@/routes/skills/SkillEditor")) },
      { path: "skills/:name", element: page(() => import("@/routes/skills/SkillDetail")) },
      { path: "connections", element: <Navigate to="/connections/providers" replace /> },
      { path: "connections/providers", element: page(() => import("@/routes/connections/Providers")) },
      { path: "connections/mcp", element: page(() => import("@/routes/connections/McpServers")) },
      { path: "connections/agents", element: page(() => import("@/routes/connections/RemoteAgents")) },
      { path: "connections/ledger", element: page(() => import("@/routes/connections/Ledger")) },
      { path: "settings", element: <Navigate to="/settings/profile" replace /> },
      { path: "settings/profile", element: page(() => import("@/routes/settings/Profile")) },
      { path: "settings/api-keys", element: page(() => import("@/routes/settings/ApiKeys")) },
      { path: "*", element: page(() => import("@/routes/NotFound")) },
    ],
  },
]);
