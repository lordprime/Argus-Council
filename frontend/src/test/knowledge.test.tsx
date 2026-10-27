import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import axe from "axe-core";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { highlight } from "@/features/knowledge/highlight";
import type { KnowledgeBase, KnowledgeDocument } from "@/features/knowledge/types";
import KnowledgePage from "@/routes/knowledge/Knowledge";

const json = (body: unknown, status = 200) =>
  new Response(status === 204 ? null : JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

type Handler = (url: string, init: RequestInit) => Response | undefined;
function mockFetch(handler: Handler) {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const res = handler(String(input), init ?? {});
    return res ?? json({ error: { code: "not_found", message: `unmocked ${String(input)}` } }, 404);
  });
}

// ---- Fake XMLHttpRequest for the progress-reporting upload.
type XhrReply = { status: number; body: unknown };
let xhrReply: (file: File) => XhrReply = () => ({ status: 201, body: {} });
const sent: { url: string; file: File }[] = [];
class FakeXHR {
  status = 0;
  responseText = "";
  url = "";
  upload: { onprogress: ((e: { lengthComputable: boolean; loaded: number; total: number }) => void) | null } = { onprogress: null };
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;
  open(_m: string, url: string) { this.url = url; }
  setRequestHeader() {}
  send(form: FormData) {
    const file = form.get("file") as File;
    sent.push({ url: this.url, file });
    setTimeout(() => {
      this.upload.onprogress?.({ lengthComputable: true, loaded: 50, total: 100 });
      setTimeout(() => {
        const r = xhrReply(file);
        this.status = r.status;
        this.responseText = JSON.stringify(r.body);
        this.onload?.();
      }, 0);
    }, 0);
  }
}

beforeEach(() => {
  sent.length = 0;
  vi.stubGlobal("XMLHttpRequest", FakeXHR);
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

const KB: KnowledgeBase = {
  id: "kb1", name: "Handbook", description: "Company policies", document_count: 1, chunk_count: 3, max_chunks: 2000,
  created_at: "2026-10-27T10:00:00Z", updated_at: "2026-10-27T10:00:00Z",
};
const DOC: KnowledgeDocument = {
  id: "d1", kb_id: "kb1", filename: "refunds.md", content_type: "text/markdown", size: 2048, sha256: "x", chunk_count: 3,
  created_at: "2026-10-27T10:00:00Z",
};

function renderPage(path = "/knowledge") {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/knowledge" element={<KnowledgePage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const baseHandler: Handler = (url) => {
  if (url === "/api/v1/knowledge-bases") return json({ knowledge_bases: [KB] });
  if (url === "/api/v1/knowledge-bases/kb1/documents") return json({ documents: [DOC] });
  if (url === "/api/v1/councils") return json([{ id: "c1", name: "Support" }]);
  return undefined;
};

describe("highlight", () => {
  it("marks words starting with query terms, case-insensitively", () => {
    expect(highlight("Refunds take 14 days; refund fast", ["refund"])).toEqual([
      { text: "Refunds", hit: true }, { text: " take 14 days; ", hit: false }, { text: "refund", hit: true }, { text: " fast", hit: false },
    ]);
    expect(highlight("abc", [])).toEqual([{ text: "abc", hit: false }]);
  });
});

describe("Knowledge page", () => {
  it("lists knowledge bases and documents with no axe violations", async () => {
    mockFetch(baseHandler);
    const { container } = renderPage();
    expect(await screen.findByRole("heading", { level: 2, name: "Handbook" })).toBeInTheDocument();
    const row = (await screen.findByText("refunds.md")).closest("tr")!;
    expect(within(row).getByText("3")).toBeInTheDocument();
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    const results = await axe.run(container);
    expect(results.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.html).join(" | ")}`)).toEqual([]);
  });

  it("uploads files with progress, refreshes documents and shows errors", async () => {
    let docs = [DOC];
    mockFetch((url, init) => (url === "/api/v1/knowledge-bases/kb1/documents" && !init.method ? json({ documents: docs }) : baseHandler(url, init)));
    xhrReply = (file) => {
      if (file.name === "huge.txt") return { status: 413, body: { error: { code: "limit_exceeded", message: "Knowledge base would exceed 2000 chunks" } } };
      const doc = { ...DOC, id: "d2", filename: file.name, chunk_count: 2 };
      docs = [doc, ...docs];
      return { status: 201, body: doc };
    };
    renderPage();
    await screen.findByText("refunds.md");
    const input = screen.getByTestId("file-input");
    fireEvent.change(input, {
      target: {
        files: [
          new File(["# Notes\nhello"], "notes.md", { type: "text/markdown" }),
          new File(["%PDF"], "paper.pdf", { type: "application/pdf" }),
          new File(["png"], "photo.png", { type: "image/png" }),
          new File(["a b c"], "huge.txt", { type: "text/plain" }),
        ],
      },
    });
    const uploads = screen.getByRole("list", { name: "Uploads" });
    expect(within(uploads).getByText(/Unsupported file type/)).toBeInTheDocument();
    expect(await within(uploads).findAllByText("Uploaded · 2 chunks")).toHaveLength(2);
    expect(await within(uploads).findByText("Knowledge base would exceed 2000 chunks")).toBeInTheDocument();
    expect(sent.map((s) => s.file.name).sort()).toEqual(["huge.txt", "notes.md", "paper.pdf"]);
    expect(sent[0]?.url).toBe("/api/v1/knowledge-bases/kb1/documents");
    expect(await screen.findByRole("cell", { name: /notes\.md/ })).toBeInTheDocument();
  });

  it("supports drag and drop", async () => {
    mockFetch(baseHandler);
    xhrReply = (file) => ({ status: 201, body: { ...DOC, id: "d3", filename: file.name, chunk_count: 1 } });
    renderPage();
    await screen.findByText("refunds.md");
    fireEvent.drop(screen.getByTestId("dropzone"), { dataTransfer: { files: [new File(["hi"], "dropped.txt", { type: "text/plain" })] } });
    expect(await screen.findByText("Uploaded · 1 chunk")).toBeInTheDocument();
  });

  it("searches and shows ranked snippets with highlighted terms", async () => {
    const fetchMock = mockFetch((url, init) => {
      if (url.startsWith("/api/v1/knowledge-bases/kb1/search?")) {
        return json({
          query: "refund days", terms: ["refund", "days"],
          results: [
            { score: 3.21, kb_id: "kb1", kb_name: "Handbook", document_id: "d1", filename: "refunds.md", chunk_index: 0,
              text: "Refunds take 14 days.", snippet: "Refunds take 14 days.", citation: "Handbook/refunds.md#0" },
            { score: 1.05, kb_id: "kb1", kb_name: "Handbook", document_id: "d1", filename: "refunds.md", chunk_index: 2,
              text: "Other days.", snippet: "Other days.", citation: "Handbook/refunds.md#2" },
          ],
        });
      }
      return baseHandler(url, init);
    });
    const { container } = renderPage();
    await userEvent.type(await screen.findByLabelText("Search this knowledge base"), "refund days");
    await userEvent.click(screen.getByRole("button", { name: "Search" }));
    const list = await screen.findByRole("list", { name: "Search results" });
    const items = within(list).getAllByRole("listitem");
    expect(items).toHaveLength(2);
    const first = items[0]!;
    expect(within(first).getByText("score 3.21")).toBeInTheDocument();
    expect(within(first).getByText("Handbook/refunds.md#0")).toBeInTheDocument();
    expect([...first.querySelectorAll("mark")].map((m) => m.textContent)).toEqual(["Refunds", "days"]);
    expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining("q=refund+days"), expect.anything());
    const results = await axe.run(container);
    expect(results.violations.map((v) => v.id)).toEqual([]);
  });

  it("memory tab lists, filters by council and deletes", async () => {
    const fetchMock = mockFetch((url, init) => {
      if (url === "/api/v1/memory/m1" && init.method === "DELETE") return json(null, 204);
      if (url.startsWith("/api/v1/memory")) {
        const all = [
          { id: "m1", council_id: "c1", key: "refunds", text: "Refund SLA is 14 days", tags: ["sla"], run_id: "r1", created_by: "Assistant", created_at: null, updated_at: null },
          { id: "m2", council_id: null, key: "", text: "Ad-hoc note", tags: [], run_id: null, created_by: "user", created_at: null, updated_at: null },
        ];
        return json({ items: url.includes("council_id=c1") ? all.slice(0, 1) : all });
      }
      return baseHandler(url, init);
    });
    const { container } = renderPage("/knowledge?tab=memory");
    const list = await screen.findByRole("list", { name: "Memory items" });
    expect(within(list).getAllByRole("listitem")).toHaveLength(2);
    expect(within(list).getByText("Support")).toBeInTheDocument();
    const results = await axe.run(container);
    expect(results.violations.map((v) => v.id)).toEqual([]);

    await screen.findByRole("option", { name: "Support" });
    await userEvent.selectOptions(screen.getByLabelText("Council"), "c1");
    await waitFor(() => expect(within(screen.getByRole("list", { name: "Memory items" })).getAllByRole("listitem")).toHaveLength(1));
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/memory?council_id=c1", expect.anything());

    await userEvent.click(screen.getByRole("button", { name: "Delete memory refunds" }));
    await userEvent.click(await screen.findByRole("button", { name: "Delete refunds" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/v1/memory/m1", expect.objectContaining({ method: "DELETE" })));
  });

  it("shows an empty state with a create action", async () => {
    const fetchMock = mockFetch((url, init) => {
      if (url === "/api/v1/knowledge-bases" && init.method === "POST") return json({ ...KB, id: "kb9", name: "Fresh" }, 201);
      if (url === "/api/v1/knowledge-bases") return json({ knowledge_bases: [] });
      return undefined;
    });
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: "New knowledge base" }));
    await userEvent.type(screen.getByLabelText(/Name/), "Fresh");
    await userEvent.click(screen.getByRole("button", { name: "Create" }));
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith("/api/v1/knowledge-bases", expect.objectContaining({ method: "POST", body: JSON.stringify({ name: "Fresh", description: "" }) })),
    );
  });
});
