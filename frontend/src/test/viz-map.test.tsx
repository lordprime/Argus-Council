import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import axe from "axe-core";
import { afterEach, describe, expect, it, vi } from "vitest";

import CouncilMap from "@/components/viz/map/CouncilMap";
import { computeLayout } from "@/components/viz/map/layout";
import type { CommEvent, NodeRef, SubCouncilInfo } from "@/features/viz/types";
import type { Member } from "@/lib/types";
import { emptyRun, type RunState } from "@/stores/run";

const member = (id: string, name: string, role: string, position: number, overseer = false): Member => ({
  id, name, role, provider: "mock", model: "m", system_prompt: "", temperature: null, position, enabled: true,
  config: overseer ? { overseer: true } : {},
});

const M = {
  chair: { kind: "member", id: "m_chair", label: "Chair", role: "chair" },
  res: { kind: "member", id: "m_res", label: "Researcher", role: "researcher" },
  cod: { kind: "member", id: "m_cod", label: "Coder", role: "coder" },
  ovs: { kind: "overseer", id: "m_ovs", label: "Monitor", role: "monitor" },
} satisfies Record<string, NodeRef>;
const USER: NodeRef = { kind: "user", id: "user", label: "You" };
const TOOL: NodeRef = { kind: "tool", id: "tool:web_search", label: "web_search" };
const MCP: NodeRef = { kind: "mcp", id: "mcp:github", label: "github" };
const REMOTE: NodeRef = { kind: "remote", id: "remote:pricing-bot", label: "pricing-bot", role: "a2a" };
const SUB: NodeRef = { kind: "subcouncil", id: "run_child", label: "Market sub-council", role: "debate", parent: "m_res" };
const SUBM: NodeRef = { kind: "submember", id: "run_child:s1", label: "Sub analyst", role: "analyst", parent: "run_child" };

let seq = 0;
const ev = (kind: CommEvent["kind"], from: NodeRef, to: NodeRef, extra: Partial<CommEvent> = {}): CommEvent => {
  seq += 1;
  return { id: `c${seq}`, seq, ts: new Date(Date.UTC(2026, 9, 27, 12, 0, seq)).toISOString(), kind, from, to, ...extra };
};

function makeRun(): RunState {
  seq = 0;
  const base = emptyRun("r1", [
    member("m_chair", "Chair", "chair", 0),
    member("m_res", "Researcher", "researcher", 1),
    member("m_cod", "Coder", "coder", 2),
    member("m_ovs", "Monitor", "monitor", 3, true),
  ]);
  const sub: SubCouncilInfo = {
    id: "run_child", task: "Size the market", mode: "debate", status: "running", depth: 1,
    members: [{ id: "s1", name: "Sub analyst", role: "analyst", overseer: false }], startedTs: "2026-10-27T12:00:05Z",
  };
  const comms: CommEvent[] = [
    ev("user", USER, M.chair, { label: "Plan a launch" }),
    ev("assign", M.chair, M.res, { label: "Research competitors" }),
    ev("thought", M.res, M.chair),
    ev("tool", M.res, TOOL, { label: "web_search", status: "running", itemId: "tc_1" }),
    ev("tool_result", TOOL, M.res, { status: "ok" }),
    ev("tool", M.cod, MCP, { label: "create_issue" }),
    ev("intercept", M.ovs, M.cod, { label: "Unsafe command" }),
    ev("protocol", M.res, REMOTE, {
      label: "tasks/send", ledger: { id: 42, hash: "abcdef0123456789", protocol: "a2a", latencyMs: 132, method: "tasks/send" },
    }),
    ev("subcouncil", M.res, SUB, { childRunId: "run_child", status: "running" }),
    ev("assign", SUB, SUBM),
  ];
  return {
    ...base,
    status: "running",
    memberStatus: { m_res: { status: "thinking" } },
    budget: {
      used: { usd: 0.05, tokens_in: 1, tokens_out: 1, steps: 1, runtime_s: 1 },
      limits: { usd: null, tokens: null, steps: 10, runtime_s: 100 },
      by_member: { m_res: { usd: 0.0123, tokens: 100 } },
    },
    comms,
    subcouncils: { run_child: sub },
  };
}

const nodeButton = (name: RegExp) => screen.getByRole("button", { name });

afterEach(() => {
  document.documentElement.removeAttribute("data-motion");
  vi.restoreAllMocks();
});

describe("CouncilMap", () => {
  it("renders platforms for members, tools, MCP, remote agents and sub-councils", () => {
    const { container } = render(<CouncilMap run={makeRun()} />);
    for (const name of [/^Chair, chair member/, /^Researcher, researcher member/, /^Coder/, /^Monitor, monitor overseer/, /^You, User/,
      /^web_search, Tool/, /^github, MCP server/, /^pricing-bot/, /^Market sub-council, Sub-council/, /^Sub analyst/]) {
      expect(nodeButton(name)).toBeInTheDocument();
    }
    expect(container.querySelector('[data-node="run_child"]')).toHaveAttribute("data-kind", "subcouncil");
    // Cost, protocol details and the ledger badge are surfaced.
    expect(nodeButton(/^Researcher/).getAttribute("aria-label")).toMatch(/cost \$0\.0123/);
    expect(nodeButton(/^pricing-bot/).getAttribute("aria-label")).toMatch(/a2a tasks\/send, 132 ms, ledger abcdef01/);
    expect(container.querySelector("[data-ledger-badge]")).not.toBeNull();
    expect(container.querySelectorAll("[data-edge]").length).toBeGreaterThan(5);
  });

  it("selects a node from the keyboard and opens its details", async () => {
    const onSelect = vi.fn();
    render(<CouncilMap run={makeRun()} onSelect={onSelect} />);
    const user = userEvent.setup();
    const remote = nodeButton(/^pricing-bot/);
    remote.focus();
    await user.keyboard("{Enter}");
    expect(onSelect).toHaveBeenCalledWith({ nodeId: "remote:pricing-bot" });
    const panel = screen.getByRole("complementary", { name: /pricing-bot details/ });
    expect(within(panel).getAllByText(/abcdef01/).length).toBeGreaterThan(0);
    expect(within(panel).getByText(/ledger #42/)).toBeInTheDocument();
    await user.click(within(panel).getByRole("button", { name: /protocol/i }));
    expect(onSelect).toHaveBeenLastCalledWith({ itemId: "c8", nodeId: "remote:pricing-bot" });
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("complementary", { name: /pricing-bot details/ })).toBeNull();
  });

  it("tabs through nodes in order", async () => {
    render(<CouncilMap run={makeRun()} />);
    const user = userEvent.setup();
    const nodes = document.querySelectorAll("[data-node]");
    (nodes[0] as SVGGElement).focus();
    await user.tab();
    expect(document.activeElement).toBe(nodes[1]);
  });

  it("renders pulse elements normally and none under reduced motion", () => {
    const { container, unmount } = render(<CouncilMap run={makeRun()} />);
    expect(container.querySelectorAll("[data-pulse]").length).toBeGreaterThan(0);
    unmount();
    document.documentElement.setAttribute("data-motion", "reduced");
    const r = render(<CouncilMap run={makeRun()} />);
    expect(r.container.querySelectorAll("[data-pulse]")).toHaveLength(0);
    expect(r.container.querySelectorAll(".animate-pulse")).toHaveLength(0);
    // Static highlight on the latest exchange instead.
    expect(r.container.querySelector("[data-glow]")).not.toBeNull();
  });

  it("pausing freezes the map and filters hide conduits", async () => {
    const run = makeRun();
    const { container, rerender } = render(<CouncilMap run={run} />);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /^live/i }));
    expect(container.querySelectorAll("[data-pulse]")).toHaveLength(0);
    const extra: NodeRef = { kind: "tool", id: "tool:shell", label: "shell" };
    act(() => rerender(<CouncilMap run={{ ...run, comms: [...run.comms, ev("tool", M.cod, extra)] }} />));
    expect(container.querySelector('[data-node="tool:shell"]')).toBeNull();
    expect(screen.getByRole("button", { name: /paused/i })).toHaveTextContent("+1");

    const before = container.querySelectorAll('[data-edge="protocol"]').length;
    expect(before).toBe(1);
    await user.click(screen.getByRole("button", { name: /^protocol/i }));
    expect(container.querySelectorAll('[data-edge="protocol"]')).toHaveLength(0);
  });

  it("has no axe violations", async () => {
    const { container } = render(<CouncilMap run={makeRun()} />);
    const results = await axe.run(container);
    expect(results.violations.map((v) => v.id)).toEqual([]);
  });

  it("lays out 2–30 nodes deterministically without overlapping platforms", () => {
    for (const n of [2, 8, 30]) {
      const nodes = Array.from({ length: n }, (_, i) => ({
        id: `n${i}`,
        kind: (["member", "tool", "mcp", "remote", "overseer"] as const)[i % 5]!,
        role: i === 0 ? "chair" : "coder",
      }));
      const a = computeLayout(nodes);
      const b = computeLayout([...nodes].reverse());
      expect([...a.nodes.values()].map((p) => [p.id, p.sx, p.sy])).toEqual(
        [...a.nodes.keys()].map((id) => [id, b.nodes.get(id)!.sx, b.nodes.get(id)!.sy]),
      );
      const ps = [...a.nodes.values()].filter((p) => p.z === 0);
      for (let i = 0; i < ps.length; i++)
        for (let j = i + 1; j < ps.length; j++)
          expect(Math.hypot(ps[i]!.gx - ps[j]!.gx, ps[i]!.gy - ps[j]!.gy)).toBeGreaterThan(ps[i]!.size + ps[j]!.size - 0.2);
    }
  });
});
