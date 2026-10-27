// Page-level consistency (docs/design/consistency-audit.md C-09 … C-32): the shared page frame,
// header, section navigation, filter bar, form actions, chips and tags, and the pages built on them.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import axe from "axe-core";
import type { ReactNode } from "react";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ConnectionsPage } from "@/components/connections/ConnectionsNav";
import { SkillCard } from "@/components/skills/SkillCard";
import {
  Button, Chip, EmptyState, FilterBar, FormActions, Page, PageHeader, SearchField, Stepper, TagList,
} from "@/components/ui";
import type { SkillSummary } from "@/lib/types";
import NotFound from "@/routes/NotFound";
import CouncilDetail from "@/routes/councils/CouncilDetail";
import Providers from "@/routes/connections/Providers";

vi.setConfig({ testTimeout: 20_000 });
afterEach(() => vi.restoreAllMocks());

const json = (body: unknown, status = 200) =>
  new Response(status === 204 ? null : JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

function mockFetch(handler: (url: string, init: RequestInit) => Response | undefined) {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) =>
    handler(String(input), init ?? {}) ?? json({ error: { code: "not_found", message: `unmocked ${String(input)}` } }, 404));
}

function renderAt(path: string, ui: ReactNode, route = "*") {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <Routes><Route path={route} element={ui} /></Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const violations = async (el: Element) =>
  (await axe.run(el)).violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.html).join(" | ")}`);

// ------------------------------------------------------------------ primitives
describe("Page and PageHeader", () => {
  it("maps the three widths and keeps one h1 with eyebrow, badge, meta and actions", async () => {
    const { container } = renderAt("/", (
      <main>
        <Page width="form" data-testid="form"><span /></Page>
        <Page data-testid="default"><span /></Page>
        <Page width="wide" data-testid="wide">
          <PageHeader eyebrow="Section" title="Title" badge={<span>Badge</span>} description="A long description."
            meta={<span>Meta</span>} actions={<Button>Act</Button>} />
        </Page>
      </main>
    ));
    expect(screen.getByTestId("form")).toHaveClass("max-w-[880px]");
    expect(screen.getByTestId("default")).toHaveClass("max-w-6xl");
    expect(screen.getByTestId("wide")).toHaveClass("max-w-none");
    for (const id of ["form", "default", "wide"]) expect(screen.getByTestId(id)).toHaveClass("p-4", "md:p-6", "mx-auto");

    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(/^Title$/);
    // The description is capped at a readable measure so the actions keep their own column.
    expect(screen.getByText("A long description.")).toHaveClass("max-w-prose");
    expect(await violations(container)).toEqual([]);
  });
});

describe("ConnectionsPage section order (C-10)", () => {
  it("renders eyebrow, h1, then the section tabs, then content", () => {
    renderAt("/connections/mcp", <ConnectionsPage title="MCP servers"><p>content</p></ConnectionsPage>);
    const eyebrow = screen.getByText("Connections", { selector: "p" });
    const h1 = screen.getByRole("heading", { level: 1, name: "MCP servers" });
    const nav = screen.getByRole("navigation", { name: "Connections" });
    const content = screen.getByText("content");
    const follows = (a: Node, b: Node) => Boolean(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
    expect(follows(eyebrow, h1)).toBe(true);
    expect(follows(h1, nav)).toBe(true);
    expect(follows(nav, content)).toBe(true);
    expect(within(nav).getByRole("link", { name: "MCP servers" })).toHaveAttribute("aria-current", "page");
  });
});

describe("FilterBar and SearchField (C-28)", () => {
  it("is an unboxed search landmark whose Clear is disabled until something is filtered", async () => {
    const onClear = vi.fn();
    const onSearch = vi.fn();
    const { container, rerender } = render(
      <main>
        <FilterBar label="Filter things" onClear={onClear} canClear={false}>
          <SearchField label="Search things" value="" onChange={onSearch} />
        </FilterBar>
      </main>,
    );
    const bar = screen.getByRole("search", { name: "Filter things" });
    expect(bar).not.toHaveClass("border");
    expect(within(bar).getByRole("button", { name: "Clear" })).toBeDisabled();
    expect(within(bar).queryByRole("button", { name: "Apply" })).toBeNull();
    await userEvent.type(screen.getByLabelText("Search things"), "x");
    expect(onSearch).toHaveBeenCalledWith("x");
    expect(await violations(container)).toEqual([]);

    rerender(
      <main>
        <FilterBar label="Filter things" onClear={onClear} canClear>
          <SearchField label="Search things" value="x" onChange={onSearch} />
        </FilterBar>
      </main>,
    );
    await userEvent.click(screen.getByRole("button", { name: "Clear" }));
    expect(onClear).toHaveBeenCalledOnce();
  });
});

describe("FormActions (C-21)", () => {
  it("puts the primary first, Cancel as a ghost link, and meta at the end", async () => {
    const { container } = renderAt("/x", (
      <main>
        <form aria-label="Example">
          <FormActions cancel={{ to: "/back" }} meta="3 seats">
            <Button type="submit" variant="primary">Save</Button>
            <Button type="button">Validate</Button>
          </FormActions>
        </form>
      </main>
    ));
    const controls = screen.getByRole("form", { name: "Example" }).querySelectorAll("button, a");
    expect([...controls].map((c) => c.textContent)).toEqual(["Save", "Validate", "Cancel"]);
    expect(screen.getByRole("link", { name: "Cancel" })).toHaveAttribute("href", "/back");
    expect(screen.getByRole("link", { name: "Cancel" })).toHaveClass("text-fg-muted");
    expect(screen.getByText("3 seats")).toHaveClass("ml-auto");
    expect(await violations(container)).toEqual([]);
  });

  it("uses a button for Cancel when it does not navigate", async () => {
    const onCancel = vi.fn();
    render(<FormActions cancel={{ onClick: onCancel }}><Button variant="primary">Save</Button></FormActions>);
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onCancel).toHaveBeenCalledOnce();
  });
});

describe("Chip, Tag and Stepper (C-20, C-24)", () => {
  it("toggles chips with aria-pressed and a non-colour check mark", async () => {
    const onPressedChange = vi.fn();
    const { container, rerender } = render(
      <main><fieldset><legend>Formats</legend><Chip pressed={false} onPressedChange={onPressedChange}>PDF</Chip></fieldset></main>,
    );
    const chip = screen.getByRole("button", { name: "PDF" });
    expect(chip).toHaveAttribute("aria-pressed", "false");
    expect(chip.querySelector("svg")).toBeNull();
    await userEvent.click(chip);
    expect(onPressedChange).toHaveBeenCalledWith(true);
    rerender(<main><fieldset><legend>Formats</legend><Chip pressed onPressedChange={onPressedChange}>PDF</Chip></fieldset></main>);
    expect(screen.getByRole("button", { name: "PDF" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "PDF" }).querySelector("svg")).not.toBeNull();
    expect(await violations(container)).toEqual([]);
  });

  it("caps a tag list with a +N more item", () => {
    render(<TagList label="Tools" items={["a", "b", "c", "d"]} max={2} />);
    const list = screen.getByRole("list", { name: "Tools" });
    expect(within(list).getAllByRole("listitem").map((li) => li.textContent)).toEqual(["a", "b", "+2 more"]);
  });

  it("bounds the shared stepper and explains the limits", async () => {
    const onChange = vi.fn();
    const { container } = render(
      <main><Stepper label="Council size" value={1} min={1} max={12} onChange={onChange} hint="Between 1 and 12 seats." /></main>,
    );
    const group = screen.getByRole("group", { name: "Council size" });
    expect(group).toHaveAccessibleDescription("Between 1 and 12 seats.");
    expect(screen.getByRole("button", { name: "Fewer seats" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "More seats" }));
    expect(onChange).toHaveBeenCalledWith(2);
    expect(await violations(container)).toEqual([]);
  });
});

describe("EmptyState and 404 (C-23)", () => {
  it("gives an empty state a real heading, h2 by default", () => {
    render(<EmptyState title="Nothing here" />);
    expect(screen.getByRole("heading", { level: 2, name: "Nothing here" })).toBeInTheDocument();
  });

  it("renders the 404 title as the only h1, with Go home and Back", async () => {
    const { container } = renderAt("/nope", <main><NotFound /></main>);
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    expect(screen.getByRole("heading", { level: 1, name: "Page not found" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Go home" })).toHaveAttribute("href", "/");
    expect(screen.getByRole("button", { name: "Back" })).toBeInTheDocument();
    expect(await violations(container)).toEqual([]);
  });
});

// ------------------------------------------------------------------ pages
describe("SkillCard (C-22)", () => {
  const skill: SkillSummary = {
    name: "source-evaluation", version: "1.0.0", source: "builtin", enabled: true, builtin: true, shadows_builtin: false, files: [],
    description: "A very long description that would otherwise run to five lines. ".repeat(6),
    allowed_tools: ["web_search", "web_fetch", "fs_read", "fs_write", "shell", "python", "git"],
  };

  it("clamps the description to three lines and pins the footer to the bottom", async () => {
    const qc = new QueryClient();
    const { container } = render(
      <QueryClientProvider client={qc}>
        <MemoryRouter><main><ul><li className="flex"><SkillCard skill={skill} /></li></ul></main></MemoryRouter>
      </QueryClientProvider>,
    );
    const card = screen.getByRole("article", { name: "source-evaluation" });
    expect(card).toHaveClass("flex", "flex-col");
    const desc = within(card).getByText(/A very long description/);
    expect(desc).toHaveClass("line-clamp-3", "min-h-[3lh]");
    expect(desc).not.toHaveClass("flex-1");
    expect(within(card).getByText("Always available").parentElement).toHaveClass("mt-auto");
    const tools = within(card).getByRole("list", { name: "Tools source-evaluation may use" });
    expect(within(tools).getByText("+2 more")).toBeInTheDocument();
    expect(await violations(container)).toEqual([]);
  });
});

describe("Providers (C-32)", () => {
  it("names the mock provider once and does not offer it under Available", async () => {
    mockFetch((url) => {
      if (url === "/api/v1/providers/kinds") {
        return json({
          kinds: [
            { key: "anthropic", label: "Anthropic", protocol: "anthropic", default_base_url: null, requires_api_key: true, self_hosted: false, suggested_models: [] },
            { key: "mock", label: "Mock (offline, deterministic)", protocol: "mock", default_base_url: null, requires_api_key: false, self_hosted: true, suggested_models: ["mock-1"] },
          ],
          system_providers: ["mock"],
        });
      }
      if (url === "/api/v1/providers") return json([]);
      return undefined;
    });
    const { container } = renderAt("/connections/providers", <main><Providers /></main>);
    const available = (await screen.findByRole("heading", { name: "Available providers" })).closest("section")!;
    expect(within(available).getByRole("button", { name: "Connect Anthropic" })).toBeInTheDocument();
    expect(within(available).queryByRole("button", { name: /Connect (Mock|Offline demo)/ })).toBeNull();
    const env = screen.getByRole("heading", { name: "Configured via environment" }).closest("section")!;
    expect(within(env).getByText("Offline demo (mock)")).toBeInTheDocument();
    expect(screen.queryByText(/Mock \(offline, deterministic\)/)).toBeNull();
    // Empty "Your providers": the header keeps the one primary Connect provider action.
    expect(screen.getAllByRole("button", { name: "Connect provider" })).toHaveLength(1);
    expect(screen.getByRole("heading", { level: 3, name: "No providers connected" })).toBeInTheDocument();
    expect(await violations(container)).toEqual([]);
  });
});

describe("CouncilDetail (C-11, C-12)", () => {
  const council = {
    id: "c1", name: "Alpha team", description: "Market research", mode: "research", min_size: 1, max_size: 6, guardrails: {},
    parent_id: null, created_at: "2026-10-01T00:00:00Z", updated_at: "2026-10-01T00:00:00Z", members: [],
  };

  it("uses the shared header without a per-page back link", async () => {
    mockFetch((url) => {
      if (url === "/api/v1/councils/c1") return json(council);
      if (url === "/api/v1/roles") return json({ roles: [], rosters: {} });
      if (url.startsWith("/api/v1/runs")) return json([]);
      if (url === "/api/v1/providers") return json([]);
      if (url === "/api/v1/providers/kinds") return json({ kinds: [], system_providers: ["mock"] });
      return undefined;
    });
    renderAt("/councils/c1", <main><CouncilDetail /></main>, "/councils/:councilId");
    expect(await screen.findByRole("heading", { level: 1, name: "Alpha team" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Councils" })).toBeNull();
    expect(screen.getByText("Research")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Start run" })).toHaveAttribute("href", "/");
    expect(screen.getByRole("button", { name: "Edit details" })).toBeInTheDocument();
    expect(screen.getByText("Market research")).toHaveClass("whitespace-pre-line");
  });

  it("renders a missing council as a single h1 empty state", async () => {
    mockFetch(() => undefined);
    const { container } = renderAt("/councils/zz", <main><CouncilDetail /></main>, "/councils/:councilId");
    expect(await screen.findByRole("heading", { level: 1, name: "Council not found" })).toBeInTheDocument();
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    expect(screen.getByRole("link", { name: "All councils" })).toHaveAttribute("href", "/councils");
    expect(await violations(container)).toEqual([]);
  });
});
