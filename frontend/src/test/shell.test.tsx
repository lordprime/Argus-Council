// App shell, breadcrumbs, session bootstrap and auth pages (consistency audit C-02, C-05, C-06, C-07,
// C-11, C-30). jsdom's matchMedia matches nothing, so the shell renders its below-md layout: tab bar,
// account-menu theme choices, drawers.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import axe from "axe-core";
import { createMemoryRouter, MemoryRouter, Route, RouterProvider, Routes, type RouteObject } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { appRoutes, RequireAuth } from "@/app/router";
import { AppShell } from "@/components/layout/AppShell";
import { authErrorsFrom, validateRegister } from "@/features/auth/validation";
import { ApiError, auth, retryAfterSeconds } from "@/lib/api";
import type { User } from "@/lib/types";
import { LoginPage, RegisterPage } from "@/routes/Auth";
import { useSession } from "@/stores/session";
import { useUi } from "@/stores/ui";

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

type Handler = (init: RequestInit) => Response;
/** Route fetch calls by "METHOD /path" (query string ignored); anything else is a 404 envelope. */
function mockApi(routes: Record<string, Handler>) {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const key = `${(init?.method ?? "GET").toUpperCase()} ${String(input).split("?")[0]}`;
    const h = routes[key];
    return h ? h(init ?? {}) : json({ error: { code: "not_found", message: key } }, 404);
  });
}

async function noAxeViolations(container: HTMLElement) {
  const results = await axe.run(container, { rules: { "color-contrast": { enabled: false } } });
  expect(results.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(", ")}`)).toEqual([]);
}

const ADA: User = { id: "u1", email: "ada@example.com", display_name: "Ada", is_admin: true, created_at: "2026-01-01T00:00:00Z" };

beforeEach(() => {
  auth.clear();
  useSession.setState({ user: ADA, status: "authenticated", bootError: null });
});
afterEach(() => vi.restoreAllMocks());

// ------------------------------------------------------------------ shell
/** The real route table (with its breadcrumb handles), each page replaced by a stub h1. */
function renderShell(path: string) {
  const stub = (r: RouteObject): RouteObject => ({ ...r, element: <h1>{`Page ${r.path ?? "home"}`}</h1> }) as RouteObject;
  const router = createMemoryRouter([{ element: <AppShell />, children: appRoutes.map(stub) }], { initialEntries: [path] });
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const utils = render(<QueryClientProvider client={qc}><RouterProvider router={router} /></QueryClientProvider>);
  return { ...utils, router };
}

describe("AppShell", () => {
  it("has a mobile tab bar with More, which opens the other sections and is active on them", async () => {
    mockApi({});
    const { router } = renderShell("/connections/mcp");
    const tabs = screen.getByRole("navigation", { name: "Primary mobile" });
    expect(within(tabs).getAllByRole("link").map((l) => l.textContent?.trim())).toEqual(["Home", "Runs", "Councils"]);
    const more = within(tabs).getByRole("button", { name: "More, current section" });
    expect(more).toHaveAttribute("aria-expanded", "false");

    await userEvent.click(more);
    const sheet = await screen.findByRole("dialog", { name: "More" });
    const links = within(sheet).getAllByRole("link");
    expect(links.map((l) => l.textContent?.trim())).toEqual(["Skills", "Knowledge", "Templates", "Connections", "Insights", "Settings"]);
    expect(within(sheet).getByRole("link", { name: "Connections" })).toHaveAttribute("aria-current", "page");
    await noAxeViolations(sheet);

    await userEvent.click(within(sheet).getByRole("link", { name: "Insights" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "More" })).not.toBeInTheDocument());
    expect(router.state.location.pathname).toBe("/insights");
    expect(within(tabs).getByRole("button", { name: "More, current section" })).toBeInTheDocument();
  });

  it("marks Runs active on a run page, and More is inactive there", () => {
    mockApi({});
    renderShell("/runs/r1");
    const tabs = screen.getByRole("navigation", { name: "Primary mobile" });
    expect(within(tabs).getByRole("link", { name: "Runs" })).toHaveAttribute("aria-current", "page");
    expect(within(tabs).getByRole("link", { name: "Home" })).not.toHaveAttribute("aria-current");
    expect(within(tabs).getByRole("button", { name: "More" })).toBeInTheDocument();
  });

  it("names its header controls by what they show, and passes axe", async () => {
    mockApi({});
    const { container } = renderShell("/");
    // Avatar: the visible initials start the name (label-content-name-mismatch).
    expect(screen.getByRole("button", { name: "AD, account menu" })).toBeInTheDocument();
    // Palette trigger is icon-only below md but keeps a name (button-name).
    expect(screen.getByRole("button", { name: /^Search or jump to…/ })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Argus Council home" })).toHaveAttribute("href", "/");
    expect(screen.getAllByRole("main")).toHaveLength(1);
    await noAxeViolations(container);
  });

  it("puts the theme choices in the account menu below md", async () => {
    mockApi({});
    const before = useUi.getState().theme;
    renderShell("/");
    expect(screen.queryByRole("button", { name: /^Theme:/ })).not.toBeInTheDocument();
    screen.getByRole("button", { name: "AD, account menu" }).focus();
    await userEvent.keyboard("{Enter}");
    await userEvent.click(await screen.findByRole("menuitemradio", { name: "Dark" }));
    expect(useUi.getState().theme).toBe("dark");
    useUi.getState().setTheme(before);
  });
});

describe("Breadcrumbs", () => {
  it("shows the route trail with a live council name, and a back link for mobile", async () => {
    mockApi({ "GET /api/v1/councils/c1": () => json({ id: "c1", name: "Alpha team", members: [] }) });
    renderShell("/councils/c1");
    const nav = screen.getByRole("navigation", { name: "Breadcrumb" });
    expect(within(nav).getByRole("link", { name: "Councils" })).toHaveAttribute("href", "/councils");
    expect(await within(nav).findByText("Alpha team")).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "Back to Councils" })).toHaveAttribute("href", "/councils");
  });

  it("reads the skill editor's mode from the query string", () => {
    mockApi({});
    renderShell("/skills/new?edit=release-notes");
    const nav = screen.getByRole("navigation", { name: "Breadcrumb" });
    expect(within(nav).getAllByRole("listitem").map((li) => li.textContent)).toEqual(["Skills", "release-notes", "Edit"]);
    expect(within(nav).getByRole("link", { name: "release-notes" })).toHaveAttribute("href", "/skills/release-notes");
  });

  it("is not shown for a top-level page, whose h1 already says where you are", () => {
    mockApi({});
    renderShell("/runs");
    expect(screen.queryByRole("navigation", { name: "Breadcrumb" })).not.toBeInTheDocument();
  });
});

// ------------------------------------------------------------------ session bootstrap
describe("session bootstrap", () => {
  beforeEach(() => {
    sessionStorage.setItem("argus.refresh", "refresh-token");
    useSession.setState({ user: null, status: "unknown", bootError: null });
  });

  it("keeps the session when the refresh is rate limited", async () => {
    mockApi({ "POST /api/v1/auth/refresh": () => json({ error: { code: "rate_limited", message: "Too many requests", details: {} } }, 429) });
    await useSession.getState().bootstrap();
    expect(useSession.getState().status).toBe("error");
    expect(useSession.getState().bootError).toMatchObject({ status: 429, code: "rate_limited" });
    expect(sessionStorage.getItem("argus.refresh")).toBe("refresh-token");
  });

  it("keeps the session when /auth/me fails with a server error", async () => {
    mockApi({
      "POST /api/v1/auth/refresh": () => json({ access_token: "a", refresh_token: "refresh-2", token_type: "bearer", expires_in: 900 }),
      "GET /api/v1/auth/me": () => json({ error: { code: "internal", message: "Database unavailable", details: {} } }, 503),
    });
    await useSession.getState().bootstrap();
    expect(useSession.getState().status).toBe("error");
    expect(sessionStorage.getItem("argus.refresh")).toBe("refresh-2");
  });

  it("keeps the session when the network is down", async () => {
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new TypeError("Failed to fetch"));
    await useSession.getState().bootstrap();
    expect(useSession.getState().status).toBe("error");
    expect(sessionStorage.getItem("argus.refresh")).toBe("refresh-token");
  });

  it("signs out only on a 401", async () => {
    mockApi({
      "POST /api/v1/auth/refresh": () => json({ error: { code: "unauthorized", message: "Refresh token already used", details: {} } }, 401),
      "GET /api/v1/auth/me": () => json({ error: { code: "unauthorized", message: "Authentication required", details: {} } }, 401),
    });
    await useSession.getState().bootstrap();
    expect(useSession.getState().status).toBe("anonymous");
    expect(sessionStorage.getItem("argus.refresh")).toBeNull();
  });

  it("shows a retryable error with a countdown for a rate limit instead of the sign-in page", async () => {
    const bootstrap = vi.fn(async () => {});
    useSession.setState({
      status: "error", bootstrap,
      bootError: new ApiError(429, "rate_limited", "Too many requests", { retry_after: 30 }),
    });
    const { container } = render(
      <MemoryRouter initialEntries={["/runs"]}>
        <Routes>
          <Route path="/login" element={<h1>Sign in</h1>} />
          <Route path="*" element={<RequireAuth />} />
        </Routes>
      </MemoryRouter>,
    );
    expect(screen.queryByRole("heading", { name: "Sign in" })).not.toBeInTheDocument();
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("Could not load your session");
    expect(alert).toHaveTextContent("Retrying in 30 s");
    expect(within(alert).getByRole("button", { name: "Retry" })).toBeDisabled();
    expect(screen.getByRole("main")).toBeInTheDocument();
    await noAxeViolations(container);
    expect(bootstrap).not.toHaveBeenCalled();
  });

  it("derives the wait from retry_after, else from the limiter's minute window", () => {
    expect(retryAfterSeconds(new ApiError(429, "rate_limited", "x", { retry_after: 12 }))).toBe(12);
    expect(retryAfterSeconds(new ApiError(429, "rate_limited", "x"), Date.UTC(2026, 0, 1, 0, 0, 45))).toBe(15);
    expect(retryAfterSeconds(new ApiError(503, "internal", "x"))).toBeNull();
  });
});

// ------------------------------------------------------------------ auth pages
function renderAuth(path: "/login" | "/register") {
  useSession.setState({ user: null, status: "anonymous", bootError: null });
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route path="/register" element={<RegisterPage />} />
        <Route path="/" element={<h1>Home page</h1>} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("auth validation", () => {
  it("checks email and password length on the client", () => {
    expect(validateRegister({ email: "", password: "", display_name: "" })).toEqual({ email: "Enter your email address", password: "Choose a password" });
    expect(validateRegister({ email: "ada", password: "short", display_name: "" })).toEqual({
      email: "Enter a valid email address, like name@example.com", password: "Use at least 12 characters (5 so far)",
    });
    expect(validateRegister({ email: "ada@example.com", password: "correct horse battery", display_name: "Ada" })).toEqual({});
  });

  it("maps server validation errors onto fields in plain words", () => {
    const err = new ApiError(422, "validation_error", "email: bad; password: short", {
      errors: [
        { loc: ["body", "email"], msg: "value is not a valid email address: An email address must have an @-sign." },
        { loc: ["body", "password"], msg: "String should have at least 12 characters" },
      ],
    });
    expect(authErrorsFrom(err)).toEqual({ email: "Enter a valid email address, like name@example.com", password: "Use at least 12 characters" });
    expect(authErrorsFrom(new ApiError(422, "validation_error", "password: String should have at least 12 characters"))).toEqual({
      password: "Use at least 12 characters",
    });
    expect(authErrorsFrom(new ApiError(409, "conflict", "Email already registered")).email).toMatch(/already exists/);
    expect(authErrorsFrom(new ApiError(401, "unauthorized", "Invalid credentials"))).toEqual({ _form: "Email or password is incorrect." });
  });
});

describe("Register page", () => {
  it("validates on the client, marks the fields and summarises the problems", async () => {
    const fetchMock = mockApi({});
    const { container } = renderAuth("/register");
    expect(screen.getByRole("main")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Create account" }));
    expect(fetchMock).not.toHaveBeenCalled();
    expect(screen.getByText("Fix 2 problems to create your account")).toBeInTheDocument();
    const email = screen.getByLabelText("Email");
    expect(email).toHaveAttribute("aria-invalid", "true");
    expect(email).toHaveAccessibleDescription("Enter your email address");
    expect(screen.getByLabelText("Password")).toHaveAttribute("aria-invalid", "true");

    await userEvent.type(email, "ada@example.com");
    expect(email).not.toHaveAttribute("aria-invalid");
    expect(screen.getByText("Fix 1 problem to create your account")).toBeInTheDocument();
    await noAxeViolations(container);
  });

  it("puts server validation errors on the fields", async () => {
    mockApi({
      "POST /api/v1/auth/register": () => json({ detail: [{ loc: ["body", "email"], msg: "value is not a valid email address: The domain name is invalid.", type: "value_error" }] }, 422),
    });
    renderAuth("/register");
    await userEvent.type(screen.getByLabelText("Email"), "ada@example.invalid");
    await userEvent.type(screen.getByLabelText("Password"), "correct horse battery");
    await userEvent.click(screen.getByRole("button", { name: "Create account" }));
    const email = screen.getByLabelText("Email");
    await waitFor(() => expect(email).toHaveAttribute("aria-invalid", "true"));
    expect(email).toHaveAccessibleDescription("Enter a valid email address, like name@example.com");
    expect(screen.queryByText(/value is not a valid email address/)).not.toBeInTheDocument();
  });

  it("shows and hides the password", async () => {
    renderAuth("/register");
    const pw = screen.getByLabelText("Password");
    expect(pw).toHaveAttribute("type", "password");
    await userEvent.click(screen.getByRole("button", { name: "Show password" }));
    expect(pw).toHaveAttribute("type", "text");
    await userEvent.click(screen.getByRole("button", { name: "Hide password" }));
    expect(pw).toHaveAttribute("type", "password");
  });
});

describe("Login page", () => {
  it("summarises wrong credentials without blaming one field", async () => {
    mockApi({ "POST /api/v1/auth/login": () => json({ error: { code: "unauthorized", message: "Invalid credentials", details: {} } }, 401) });
    const { container } = renderAuth("/login");
    await userEvent.type(screen.getByLabelText("Email"), "ada@example.com");
    await userEvent.type(screen.getByLabelText("Password"), "not the password");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByText("Email or password is incorrect.")).toBeInTheDocument();
    expect(screen.getByText("Could not sign in")).toBeInTheDocument();
    expect(screen.getByLabelText("Email")).not.toHaveAttribute("aria-invalid");
    await noAxeViolations(container);
  });

  it("counts down a rate limit and blocks submit until it ends", async () => {
    mockApi({
      "POST /api/v1/auth/login": () => json({ error: { code: "rate_limited", message: "Too many attempts, slow down", details: { retry_after: 30 } } }, 429),
    });
    renderAuth("/login");
    await userEvent.type(screen.getByLabelText("Email"), "ada@example.com");
    await userEvent.type(screen.getByLabelText("Password"), "whatever it is");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByText("Too many attempts")).toBeInTheDocument();
    expect(screen.getByText(/then try again/)).toHaveTextContent("Wait 30 s, then try again.");
    expect(screen.getByRole("button", { name: "Sign in" })).toBeDisabled();
  });
});
