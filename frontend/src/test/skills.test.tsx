import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import axe from "axe-core";
import type { ReactNode } from "react";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import { composeSkillMd, demoteHeadings, filePathError, stripFrontmatter } from "@/features/skills/skillmd";
import type { SkillDetail, SkillSummary } from "@/lib/types";
import SkillDetailPage from "@/routes/skills/SkillDetail";
import SkillEditor from "@/routes/skills/SkillEditor";
import SkillsList from "@/routes/skills/SkillsList";

afterEach(() => vi.restoreAllMocks());

const json = (body: unknown, status = 200) =>
  new Response(status === 204 ? null : JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

type Handler = (url: string, init: RequestInit) => Response | undefined;
function mockFetch(handler: Handler) {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const res = handler(String(input), init ?? {});
    return res ?? json({ error: { code: "not_found", message: `unmocked ${String(input)}` } }, 404);
  });
}

function renderAt(path: string, element: ReactNode, route: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path={route} element={element} />
          <Route path="/skills" element={<p>skills index</p>} />
          <Route path="/skills/:name" element={<p>skill page</p>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const summary = (over: Partial<SkillSummary>): SkillSummary => ({
  name: "code-review", description: "Review code for correctness.", version: "1.0.0", allowed_tools: ["fs_read"], files: [],
  source: "builtin", enabled: true, builtin: true, shadows_builtin: false, ...over,
});

const LIST = [
  summary({}),
  summary({ name: "release-notes", description: "Write release notes from merged PRs.", source: "created", builtin: false, allowed_tools: [] }),
];

describe("SkillsList", () => {
  it("shows built-in and user badges, filters by search, and has no axe violations", async () => {
    mockFetch((url) => (url === "/api/v1/skills" ? json({ skills: LIST }) : undefined));
    const { container } = renderAt("/skills", <SkillsList />, "/skills");
    expect(await screen.findByRole("link", { name: "code-review" })).toBeInTheDocument();
    const userCard = screen.getByRole("article", { name: "release-notes" });
    expect(within(userCard).getByText("Yours")).toBeInTheDocument();
    expect(within(userCard).getByRole("switch", { name: "Enabled (release-notes)" })).toHaveAttribute("aria-checked", "true");
    expect(within(screen.getByRole("article", { name: "code-review" })).getByText("Built-in")).toBeInTheDocument();
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);

    const results = await axe.run(container);
    expect(results.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.html).join(" | ")}`)).toEqual([]);

    await userEvent.type(screen.getByLabelText("Search skills"), "release");
    expect(screen.queryByRole("link", { name: "code-review" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "release-notes" })).toBeInTheDocument();
  });

  it("toggles a user skill with PATCH", async () => {
    const fetchMock = mockFetch((url, init) => {
      if (url === "/api/v1/skills" && !init.method) return json({ skills: LIST });
      if (url === "/api/v1/skills/release-notes" && init.method === "PATCH") return json({ name: "release-notes", enabled: false });
      return undefined;
    });
    renderAt("/skills", <SkillsList />, "/skills");
    await userEvent.click(await screen.findByRole("switch", { name: "Enabled (release-notes)" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/v1/skills/release-notes", expect.objectContaining({ method: "PATCH" })));
    const call = fetchMock.mock.calls.find(([u, i]) => u === "/api/v1/skills/release-notes" && i?.method === "PATCH");
    expect(JSON.parse(String(call?.[1]?.body))).toEqual({ enabled: false });
  });
});

describe("SkillEditor", () => {
  it("shows client-side errors on save and server validation errors on Validate", async () => {
    const fetchMock = mockFetch((url) =>
      url === "/api/v1/skills/validate" ? json({ valid: false, errors: ["description is required (it tells agents when to use the skill)"] }) : undefined);
    renderAt("/skills/new", <SkillEditor />, "/skills/new");

    await userEvent.click(screen.getByRole("button", { name: "Create skill" }));
    const summaryBox = await screen.findByText(/Fix 3 problems before saving/);
    expect(summaryBox).toBeInTheDocument();
    expect(screen.getAllByText(/lowercase letters, digits and hyphens/i).length).toBeGreaterThan(0);
    expect(fetchMock).not.toHaveBeenCalledWith("/api/v1/skills", expect.anything());

    await userEvent.click(screen.getByRole("button", { name: "Test with validate" }));
    expect(await screen.findByText("The server rejected this package")).toBeInTheDocument();
    expect(screen.getByText("description is required (it tells agents when to use the skill)")).toBeInTheDocument();
  });

  it("customizing a built-in prefills the form and POSTs a user copy with the same name", async () => {
    const builtin: SkillDetail = {
      ...summary({ files: ["references/severity.md"] }),
      skill_md: "---\nname: code-review\ndescription: Review code.\nversion: 1.0.0\nallowed-tools: [fs_read]\nmetadata:\n  builtin: true\n---\n\n# Code review\n\nCheck it.\n",
      file_contents: { "references/severity.md": "# Severity" },
    };
    const fetchMock = mockFetch((url, init) => {
      if (url === "/api/v1/skills/code-review") return json(builtin);
      if (url === "/api/v1/skills" && init.method === "POST") return json(summary({ source: "created", builtin: false, shadows_builtin: true }), 201);
      return undefined;
    });
    renderAt("/skills/new?from=code-review", <SkillEditor />, "/skills/new");
    expect(await screen.findByRole("heading", { level: 1, name: "Customize code-review" })).toBeInTheDocument();
    expect(await screen.findByLabelText("Name")).toHaveValue("code-review");
    expect(screen.getByRole("button", { name: "fs_read" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByLabelText("File 1 path")).toHaveValue("references/severity.md");

    await userEvent.click(screen.getByRole("button", { name: "Create my copy" }));
    expect(await screen.findByText("skill page")).toBeInTheDocument();
    const post = fetchMock.mock.calls.find(([u, i]) => u === "/api/v1/skills" && i?.method === "POST");
    const body = JSON.parse(String(post?.[1]?.body)) as { skill_md: string; files: Record<string, string> };
    expect(body.skill_md).toContain('name: "code-review"');
    expect(body.skill_md).not.toContain("builtin: true");
    expect(body.skill_md).toContain("# Code review");
    expect(body.files).toEqual({ "references/severity.md": "# Severity" });
  });
});

describe("SkillDetail", () => {
  it("renders the SKILL.md body below the page h1 and confirms delete by name", async () => {
    const mine: SkillDetail = {
      ...summary({ name: "release-notes", source: "created", builtin: false, files: ["template.md"] }),
      skill_md: "---\nname: release-notes\ndescription: Notes.\n---\n\n# Release notes\n\n<script>alert(1)</script>Use **bold**.\n",
      file_contents: { "template.md": "## Added\n" },
    };
    const fetchMock = mockFetch((url, init) => {
      if (url === "/api/v1/skills/release-notes" && init.method === "DELETE") return json(null, 204);
      if (url === "/api/v1/skills/release-notes") return json(mine);
      return undefined;
    });
    const { container } = renderAt("/skills/release-notes", <SkillDetailPage />, "/skills/:name");
    expect(await screen.findByRole("heading", { level: 1, name: "release-notes" })).toBeInTheDocument();
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    expect(screen.getByRole("heading", { level: 3, name: "Release notes" })).toBeInTheDocument();
    expect(container.querySelector("script")).toBeNull();
    expect(screen.getByRole("region", { name: "Contents of template.md" })).toHaveTextContent("## Added");

    await userEvent.click(screen.getByRole("button", { name: "Delete" }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("Delete skill “release-notes”?");
    expect(fetchMock).not.toHaveBeenCalledWith("/api/v1/skills/release-notes", expect.objectContaining({ method: "DELETE" }));
    await userEvent.click(within(dialog).getByRole("button", { name: "Delete release-notes" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/v1/skills/release-notes", expect.objectContaining({ method: "DELETE" })));
    expect(await screen.findByText("skills index")).toBeInTheDocument();
  });
});

describe("skillmd helpers", () => {
  it("composes quoted frontmatter, strips it, demotes headings outside code fences, and checks file paths", () => {
    const md = composeSkillMd({ name: "a-b", description: 'Use when: "x"', version: "2.0.0", allowedTools: ["shell"], body: "# T\n\nBody" });
    expect(md).toBe('---\nname: "a-b"\ndescription: "Use when: \\"x\\""\nversion: "2.0.0"\nallowed-tools: ["shell"]\n---\n\n# T\n\nBody\n');
    expect(stripFrontmatter(md)).toBe("# T\n\nBody");
    expect(demoteHeadings("# A\n```\n# not a heading\n```\n## B")).toBe("### A\n```\n# not a heading\n```\n#### B");
    expect(filePathError("../etc/passwd.txt")).toMatch(/relative path/);
    expect(filePathError("img.png")).toMatch(/text files/);
    expect(filePathError("references/a.md")).toBeUndefined();
  });
});
