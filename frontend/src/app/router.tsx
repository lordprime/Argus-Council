import { lazy, Suspense, type ComponentType } from "react";
import { createBrowserRouter, Navigate, Outlet, useLocation, type RouteObject } from "react-router";

import { AppShell } from "@/components/layout/AppShell";
import { CouncilNameCrumb, RunTopicCrumb, type Crumb, type RouteHandle } from "@/components/layout/Breadcrumbs";
import { Button, Card, ErrorState, Skeleton } from "@/components/ui";
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

/**
 * The session check failed for a reason other than an invalid session (429, 5xx, offline): keep the
 * tokens and offer a retry instead of bouncing to /login (audit C-05). A rate limit counts down and
 * then retries by itself.
 */
export function SessionError() {
  const { bootError, bootstrap, logout } = useSession();
  return (
    <main id="main" className="min-h-screen flex items-center justify-center p-4">
      <Card className="w-full max-w-md">
        <h1 className="sr-only">Argus Council</h1>
        <ErrorState error={bootError} title="Could not load your session" onRetry={() => void bootstrap()} autoRetry />
        <div className="flex justify-center pb-6 -mt-4">
          <Button variant="ghost" size="sm" onClick={logout}>Sign in again</Button>
        </div>
      </Card>
    </main>
  );
}

export function RequireAuth() {
  const { status } = useSession();
  const loc = useLocation();
  if (status === "unknown") return <div className="p-6"><Skeleton className="h-10 w-full" /></div>;
  if (status === "error") return <SessionError />;
  if (status === "anonymous") return <Navigate to={`/login?next=${encodeURIComponent(loc.pathname + loc.search)}`} replace />;
  return (
    <AppShell>
      <Outlet />
    </AppShell>
  );
}

// ------------------------------------------------------------------ breadcrumbs (route handle.crumbs)
const trail = (...crumbs: Crumb[]): RouteHandle => ({ crumbs: () => crumbs });
const RUNS: Crumb = { label: "Runs", to: "/runs" };
const COUNCILS: Crumb = { label: "Councils", to: "/councils" };
const SKILLS: Crumb = { label: "Skills", to: "/skills" };
const INSIGHTS: Crumb = { label: "Insights", to: "/insights" };
const CONNECTIONS: Crumb = { label: "Connections", to: "/connections/providers" };
const SETTINGS: Crumb = { label: "Settings", to: "/settings/profile" };

const runCrumbs: RouteHandle = {
  crumbs: ({ params }) => {
    const id = params.runId ?? "";
    const topic = <RunTopicCrumb runId={id} />;
    return params["*"] === "output"
      ? [RUNS, { label: topic, to: `/runs/${id}` }, { label: "Deliverable" }]
      : [RUNS, { label: topic }];
  },
};

const skillEditorCrumbs: RouteHandle = {
  crumbs: ({ search }) => {
    const edit = search.get("edit");
    const from = search.get("from");
    const source = edit ?? from;
    if (!source) return [SKILLS, { label: "New skill" }];
    return [SKILLS, { label: source, to: `/skills/${encodeURIComponent(source)}` }, { label: edit ? "Edit" : "Customize" }];
  },
};

/** The signed-in app's routes (exported so tests can mount them in a memory router). */
export const appRoutes: RouteObject[] = [
  { index: true, element: page(() => import("@/routes/Home")) },
  { path: "runs", element: page(() => import("@/routes/RunsList")), handle: trail({ label: "Runs" }) },
  { path: "runs/:runId/*", element: page(() => import("@/routes/RunWorkspace")), handle: runCrumbs },
  { path: "councils", element: page(() => import("@/routes/councils/CouncilsList")), handle: trail({ label: "Councils" }) },
  { path: "councils/new", element: page(() => import("@/routes/councils/CouncilBuilder")), handle: trail(COUNCILS, { label: "New council" }) },
  {
    path: "councils/:councilId",
    element: page(() => import("@/routes/councils/CouncilDetail")),
    handle: { crumbs: ({ params }) => [COUNCILS, { label: <CouncilNameCrumb councilId={params.councilId ?? ""} /> }] } satisfies RouteHandle,
  },
  { path: "skills", element: page(() => import("@/routes/skills/SkillsList")), handle: trail({ label: "Skills" }) },
  { path: "skills/new", element: page(() => import("@/routes/skills/SkillEditor")), handle: skillEditorCrumbs },
  {
    path: "skills/:name",
    element: page(() => import("@/routes/skills/SkillDetail")),
    handle: { crumbs: ({ params }) => [SKILLS, { label: params.name ?? "Skill" }] } satisfies RouteHandle,
  },
  { path: "knowledge", element: page(() => import("@/routes/knowledge/Knowledge")), handle: trail({ label: "Knowledge" }) },
  { path: "templates", element: page(() => import("@/routes/templates/Templates")), handle: trail({ label: "Templates" }) },
  { path: "connections", element: <Navigate to="/connections/providers" replace /> },
  { path: "connections/providers", element: page(() => import("@/routes/connections/Providers")), handle: trail(CONNECTIONS, { label: "Providers" }) },
  { path: "connections/cli-agents", element: page(() => import("@/routes/connections/CliAgents")), handle: trail(CONNECTIONS, { label: "CLI agents" }) },
  { path: "connections/mcp", element: page(() => import("@/routes/connections/McpServers")), handle: trail(CONNECTIONS, { label: "MCP servers" }) },
  { path: "connections/agents", element: page(() => import("@/routes/connections/RemoteAgents")), handle: trail(CONNECTIONS, { label: "Remote agents" }) },
  { path: "connections/ledger", element: page(() => import("@/routes/connections/Ledger")), handle: trail(CONNECTIONS, { label: "Ledger" }) },
  { path: "insights", element: page(() => import("@/routes/insights/Insights")), handle: trail(INSIGHTS, { label: "Overview" }) },
  { path: "insights/audit", element: page(() => import("@/routes/insights/Audit")), handle: trail(INSIGHTS, { label: "Audit log" }) },
  { path: "settings", element: <Navigate to="/settings/profile" replace /> },
  { path: "settings/profile", element: page(() => import("@/routes/settings/Profile")), handle: trail(SETTINGS, { label: "Profile" }) },
  { path: "settings/api-keys", element: page(() => import("@/routes/settings/ApiKeys")), handle: trail(SETTINGS, { label: "API keys" }) },
  { path: "settings/webhooks", element: page(() => import("@/routes/settings/Webhooks")), handle: trail(SETTINGS, { label: "Webhooks" }) },
  { path: "settings/data", element: page(() => import("@/routes/settings/Data")), handle: trail(SETTINGS, { label: "Data & privacy" }) },
  { path: "settings/users", element: page(() => import("@/routes/settings/Users")), handle: trail(SETTINGS, { label: "Users" }) },
  { path: "*", element: page(() => import("@/routes/NotFound")), handle: trail({ label: "Page not found" }) },
];

export const router = createBrowserRouter([
  { path: "/login", element: <LoginPage /> },
  { path: "/register", element: <RegisterPage /> },
  { element: <RequireAuth />, children: appRoutes },
]);
