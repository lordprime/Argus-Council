import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import axe from "axe-core";
import type { ReactNode } from "react";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import { lineDiff, diffStats } from "@/features/skills/diff";
import type { MarketplaceSkill, SkillVersionDetail, SkillVersionMeta } from "@/features/skills/queries";
import { frontmatterChecks } from "@/features/skills/skillmd";
import type { SkillDetail, SkillSummary } from "@/lib/types";
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
          <Route path="/skills/:name" element={<p>skill page</p>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const axeClean = async (el: Element) => {
  const results = await axe.run(el);
  expect(results.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.html).join(" | ")}`)).toEqual([]);
};

const market = (over: Partial<MarketplaceSkill>): MarketplaceSkill => ({
  name: "threat-model", description: "Build a STRIDE threat model. Use when designing.", version: "1.0.0", tags: ["security"],
  allowed_tools: ["fs_read"], files: ["references/stride.md"], installed: false, installed_version: null, installed_from_marketplace: false, ...over,
});

describe("Marketplace tab", () => {
  it("lists catalogue skills, installs one and shows the installed badge", async () => {
    const mine: SkillSummary[] = [];
    let installed = false;
    const fetchMock = mockFetch((url, init) => {
      if (url === "/api/v1/skills") return json({ skills: mine });
      if (url === "/api/v1/skills/marketplace") {
        return json({ skills: [market({ installed, installed_version: installed ? "1.0.0" : null }), market({ name: "adr-writer", tags: ["architecture"], installed: true, installed_version: "1.0.0" })] });
      }
      if (url === "/api/v1/skills/marketplace/threat-model/install" && init.method === "POST") {
        installed = true;
        return json({ name: "threat-model", description: "x", version: "1.0.0", allowed_tools: [], files: [], source: "marketplace", enabled: true, builtin: false, shadows_builtin: false }, 201);
      }
      return undefined;
    });
    const { container } = renderAt("/skills?tab=marketplace", <SkillsList />, "/skills");
    expect(screen.getByRole("tab", { name: "Marketplace" })).toHaveAttribute("aria-selected", "true");
    const card = await screen.findByRole("article", { name: "threat-model" });
    const other = screen.getByRole("article", { name: "adr-writer" });
    expect(within(other).getByText("Installed")).toBeInTheDocument();
    expect(within(other).queryByRole("button", { name: /Install/ })).not.toBeInTheDocument();
    await axeClean(container);

    await userEvent.click(within(card).getByRole("button", { name: "Install threat-model" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/v1/skills/marketplace/threat-model/install", expect.objectContaining({ method: "POST" })));
    expect(await within(card).findByText("Installed")).toBeInTheDocument();
    expect(within(card).getByRole("link", { name: "Open threat-model" })).toHaveAttribute("href", "/skills/threat-model");
  });

  it("filters the catalogue by tag", async () => {
    mockFetch((url) => {
      if (url === "/api/v1/skills") return json({ skills: [] });
      if (url === "/api/v1/skills/marketplace") return json({ skills: [market({}), market({ name: "adr-writer", tags: ["architecture"] })] });
      return undefined;
    });
    renderAt("/skills?tab=marketplace", <SkillsList />, "/skills");
    await screen.findByRole("article", { name: "adr-writer" });
    await userEvent.type(screen.getByLabelText("Search marketplace"), "architecture");
    expect(screen.queryByRole("article", { name: "threat-model" })).not.toBeInTheDocument();
  });
});

const MD1 = "---\nname: release-notes\ndescription: Notes. Use when releasing.\nversion: 1.0.0\n---\n\n# Release notes\n\nOld step.\n";
const MD2 = MD1.replace("Old step.", "New step.").replace("version: 1.0.0", "version: 1.1.0");

describe("Version history", () => {
  it("diffs an older version against current and restores it", async () => {
    let current = MD2;
    const detail = (): SkillDetail => ({
      name: "release-notes", description: "Notes. Use when releasing.", version: current === MD1 ? "1.0.0" : "1.1.0", allowed_tools: [], files: [],
      source: "created", enabled: true, builtin: false, shadows_builtin: false, skill_md: current, file_contents: {},
    });
    const meta = (n: number, note: string): SkillVersionMeta => ({ number: n, version: n === 1 ? "1.0.0" : "1.1.0", created_at: new Date().toISOString(), author: "a@example.com", note, files: [] });
    let versions = [meta(2, "updated"), meta(1, "created")];
    const v1: SkillVersionDetail = { ...meta(1, "created"), name: "release-notes", skill_md: MD1, file_contents: {} };
    const fetchMock = mockFetch((url, init) => {
      if (url === "/api/v1/skills/release-notes") return json(detail());
      if (url === "/api/v1/skills/release-notes/versions") return json({ name: "release-notes", current: versions[0]!.number, versions });
      if (url === "/api/v1/skills/release-notes/versions/1") return json(v1);
      if (url === "/api/v1/skills/release-notes/versions/1/restore" && init.method === "POST") {
        current = MD1;
        versions = [meta(3, "restored from v1"), ...versions];
        return json({ ...detail(), restored_from: 1, current_version: 3, version_count: 3 });
      }
      return undefined;
    });
    const { container } = renderAt("/skills/new?edit=release-notes", <SkillEditor />, "/skills/new");
    expect(await screen.findByRole("heading", { level: 2, name: "Version history" })).toBeInTheDocument();
    const diff = await screen.findByRole("region", { name: "Diff of version 1 against current" });
    expect(diff).toHaveTextContent("removed: Old step.");
    expect(diff).toHaveTextContent("added: New step.");
    expect(screen.getByLabelText("Instructions preview")).toBeInTheDocument();
    await axeClean(container);

    await userEvent.click(screen.getByRole("button", { name: "Restore version 1" }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("Restore release-notes to v1?");
    await userEvent.click(within(dialog).getByRole("button", { name: "Restore v1" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/v1/skills/release-notes/versions/1/restore", expect.objectContaining({ method: "POST" })));
    // The editor re-initialises from the restored content.
    await waitFor(() => expect(screen.getByLabelText("SKILL.md body")).toHaveValue("# Release notes\n\nOld step."));
    expect(await screen.findByText("restored from v1", { exact: false })).toBeInTheDocument();
  });
});

describe("Authoring helpers", () => {
  it("gives live frontmatter feedback and a source preview", async () => {
    mockFetch(() => undefined);
    renderAt("/skills/new", <SkillEditor />, "/skills/new");
    const status = screen.getByRole("region", { name: /Frontmatter check/ });
    expect(status).toHaveTextContent("3 errors");
    await userEvent.type(screen.getByLabelText("Name"), "my-skill");
    await userEvent.type(screen.getByLabelText("Description"), "Does a thing. Use when needed.");
    await userEvent.type(screen.getByLabelText("SKILL.md body"), "Step one.");
    expect(status).toHaveTextContent("looks valid");
    await userEvent.click(screen.getByRole("radio", { name: "SKILL.md" }));
    expect(screen.getByRole("region", { name: "SKILL.md source preview" })).toHaveTextContent('name: "my-skill"');
  });

  it("computes a line diff and checks frontmatter", () => {
    const ops = lineDiff("a\nb\nc", "a\nx\nc\nd");
    expect(ops).toEqual([
      { kind: "same", text: "a" }, { kind: "del", text: "b" }, { kind: "add", text: "x" }, { kind: "same", text: "c" }, { kind: "add", text: "d" },
    ]);
    expect(diffStats(ops)).toEqual({ added: 2, removed: 1 });
    const checks = frontmatterChecks({ name: "Bad", description: "", version: "x", allowedTools: ["mcp.tool"], body: "b" });
    expect(Object.fromEntries(checks.map((c) => [c.field, c.status]))).toEqual({
      name: "error", description: "error", version: "warn", "allowed-tools": "warn", body: "ok",
    });
  });
});
