// Skill trace in the run view and the ledger: expandable skill rows, skill chips on tool calls, the
// Skills view and the ledger's Skill filter.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SimpleRow, ToolChip } from "@/components/run/parts";
import { SkillsView } from "@/components/run/SkillsView";
import type { Member, RunEvent } from "@/lib/types";
import Ledger from "@/routes/connections/Ledger";
import { emptyRun, reduceAll, type SimpleItem, type ToolCallItem } from "@/stores/run";

afterEach(() => vi.restoreAllMocks());

let seq = 0;
const ev = (type: string, data: Record<string, unknown>, member = "m1", name = "Ana"): RunEvent => ({
  v: 1, seq: ++seq, id: `e${seq}`, run_id: "r1", ts: `2026-01-01T00:00:0${seq % 10}Z`, type,
  actor: { kind: "agent", member_id: member, role: "analyst", name }, data,
});

const MEMBERS = [
  { id: "m1", name: "Ana", role: "analyst" }, { id: "m2", name: "Ben", role: "writer" },
] as unknown as Member[];

const text = (body: string) => new Response(body, { status: 200, headers: { "content-type": "text/plain" } });

describe("skill trace", () => {
  it("skill.loaded row expands to the saved instructions", async () => {
    seq = 0;
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input) =>
      String(input).startsWith("/api/v1/runs/r1/artifacts/content?path=skills%2Fbrief%2FSKILL.md") ? text("Do X") : new Response("nope", { status: 404 }));
    const s = reduceAll(emptyRun("r1", MEMBERS), [ev("skill.loaded", {
      skill_id: "sk1", name: "brief", member_id: "m1", version: "1.0.0", tool_call_id: "tc1", artifact_path: "skills/brief/SKILL.md",
      sha256: "a".repeat(64), bytes: 4, files: ["ref.md"], expected_tools: [],
    })]);
    const item = Object.values(s.items)[0] as SimpleItem;
    render(<SimpleRow item={item} runId="r1" onOpenArtifact={() => {}} />);
    expect(screen.getByText(/Loaded skill/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Show" }));
    expect(await screen.findByText("Do X")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalled();
  });

  it("retrying after a failed fetch clears the error", async () => {
    seq = 0;
    let calls = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation(async () => (++calls === 1 ? new Response("boom", { status: 500 }) : text("Do X")));
    const s = reduceAll(emptyRun("r1", MEMBERS), [ev("skill.loaded", {
      skill_id: "sk1", name: "brief", member_id: "m1", version: "1.0.0", artifact_path: "skills/brief/SKILL.md",
    })]);
    render(<SimpleRow item={Object.values(s.items)[0] as SimpleItem} runId="r1" onOpenArtifact={() => {}} />);
    await userEvent.click(screen.getByRole("button", { name: "Show" }));
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Hide" }));
    await userEvent.click(screen.getByRole("button", { name: "Show" }));
    expect(await screen.findByText("Do X")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("says when the content was not saved", () => {
    seq = 0;
    const s = reduceAll(emptyRun("r1", MEMBERS), [ev("skill.loaded", {
      skill_id: "sk1", name: "brief", member_id: "m1", version: "1.0.0", tool_call_id: "tc1", artifact_path: null,
    })]);
    render(<SimpleRow item={Object.values(s.items)[0] as SimpleItem} runId="r1" onOpenArtifact={() => {}} />);
    expect(screen.getByText("Content not saved")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Show" })).not.toBeInTheDocument();
  });

  it("old skill.loaded without artifact_path renders without expand", () => {
    seq = 0;
    const s = reduceAll(emptyRun("r1", MEMBERS), [ev("skill.loaded", { skill_id: "sk1", name: "brief", member_id: "m1", version: "1.0.0" })]);
    render(<SimpleRow item={Object.values(s.items)[0] as SimpleItem} runId="r1" onOpenArtifact={() => {}} />);
    expect(screen.queryByRole("button", { name: "Show" })).not.toBeInTheDocument();
    expect(screen.queryByText("Content not saved")).not.toBeInTheDocument();
    expect(screen.getByText(/Loaded skill/)).toHaveTextContent("Loaded skill brief");
  });

  it("tool call rows show skill chips", () => {
    seq = 0;
    const s = reduceAll(emptyRun("r1", MEMBERS), [ev("tool.call.started", {
      tool_call_id: "tc2", tool: "web_search", source: "builtin", args: {}, active_skills: ["brief"],
    })]);
    render(<ToolChip t={s.items.tc2 as ToolCallItem} onSelect={() => {}} />);
    expect(screen.getByText("brief")).toBeInTheDocument();
  });

  it("Skills view groups by skill with who used it", async () => {
    seq = 0;
    const s = reduceAll(emptyRun("r1", MEMBERS), [
      ev("skill.loaded", { skill_id: "sk1", name: "brief", member_id: "m1", version: "1.0.0", artifact_path: null }),
      ev("skill.loaded", { skill_id: "sk1", name: "brief", member_id: "m2", version: "1.0.0", artifact_path: null }, "m2", "Ben"),
      ev("skill.read", { name: "brief", version: "1.0.0", member_id: "m1", tool_call_id: "tc3", path: "ref.md", artifact_path: null }),
      ev("tool.call.started", { tool_call_id: "tc4", tool: "web_search", source: "builtin", args: {}, active_skills: ["brief"] }),
    ]);
    expect(s.skills).toHaveLength(3);
    render(<SkillsView run={s} />);
    const group = screen.getByRole("region", { name: "brief" });
    expect(screen.getAllByRole("region", { name: "brief" })).toHaveLength(1);
    expect(within(group).getByText("Ana")).toBeInTheDocument();
    expect(within(group).getByText("Ben")).toBeInTheDocument();
    expect(within(group).getByText("ref.md")).toBeInTheDocument();
    expect(within(group).getByText(/1 tool call/)).toBeInTheDocument();
  });

  it("ledger filter offers Skill", () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async () =>
      new Response(JSON.stringify({ entries: [] }), { status: 200, headers: { "content-type": "application/json" } }));
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const ui: ReactNode = <QueryClientProvider client={qc}><MemoryRouter><Ledger /></MemoryRouter></QueryClientProvider>;
    render(ui);
    return waitFor(() => expect(screen.getByRole("radio", { name: "Skill" })).toBeInTheDocument());
  });
});
