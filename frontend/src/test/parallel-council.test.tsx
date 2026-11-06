// The parallel, debating council (docs/design/parallel-council.md): the run store's debate, helper,
// live and context events, the Debate view, helpers in the council rail, timeline rows, the
// communication edges, the secret devil's advocate, and the new guardrail fields.

import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import axe from "axe-core";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import { GuardrailsEditor } from "@/components/councils/GuardrailsEditor";
import { ADVOCATE_SECRET, DebatePanel } from "@/components/run/debate";
import { CouncilRail, InterventionBar, SimpleRow } from "@/components/run/parts";
import type { Member, RunEvent } from "@/lib/types";
import { challengeState } from "@/stores/debate";
import { contextCharsSaved, emptyRun, reduceAll, useRuns, type RunState, type SimpleItem } from "@/stores/run";

let seq = 0;
const ev = (type: string, data: Record<string, unknown>, extra: Partial<RunEvent> = {}): RunEvent => ({
  v: 1, seq: ++seq, id: `e${seq}`, run_id: "r", ts: "2026-11-01T00:00:00Z", type, actor: { kind: "system", name: "Argus" }, data, ...extra,
});
const member = (id: string, name: string, role: string, position: number): Member => ({
  id, name, role, provider: "mock", model: "mock-1", system_prompt: "", temperature: null, position, enabled: true, config: {},
});
const MEMBERS = [member("m1", "Ada", "researcher", 0), member("m2", "Ben", "analyst", 1), member("m3", "Cy", "writer", 2)];
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

function debateRun(): RunState {
  seq = 0;
  return reduceAll({ ...emptyRun("r", MEMBERS), status: "running" }, [
    ev("council.role_assigned", { member_id: "m2", name: "Ben", stance: "devils_advocate", secret: true, pool: ["m1", "m2", "m3"] }),
    ev("debate.claim", { claim_id: "clm_1", author_id: "m1", statement: "Solar capacity grew 41 GW in 2024", evidence: [{ source: "[Source 1]", quote: "41 GW" }], confidence: 0.8, status: "open" }),
    ev("debate.claim", { claim_id: "clm_2", author_id: "m3", statement: "Wind will overtake solar by 2027", evidence: [], confidence: 0.4, status: "open" }),
    ev("debate.challenge", { challenge_id: "chl_1", claim_id: "clm_1", challenger_id: "m2", target_id: "m1", objection: "That figure looks like 2023", demand: "proof", round: 1 }),
    // A defence of a proof demand with no evidence: unsupported, still open.
    ev("debate.response", { challenge_id: "chl_1", responder_id: "m1", stance: "defend", argument: "I am confident it is right", evidence: [], unsupported: true }),
    ev("debate.response", { challenge_id: "chl_1", responder_id: "m1", stance: "defend", argument: "IEA table 3 says 2024", evidence: [{ source: "https://iea.org/t3" }], unsupported: false }),
    ev("debate.resolution", { challenge_id: "chl_1", outcome: "accepted", note: "Table 3 confirms it" }),
    ev("debate.claim_status", { claim_id: "clm_1", status: "supported" }),
    ev("debate.challenge", { challenge_id: "chl_2", claim_id: "clm_2", challenger_id: "m1", target_id: "m3", objection: "No source", demand: "counterexample", round: 1 }),
    ev("debate.round", { phase: "deliberate", round: 1, status: "started", open_challenges: 1 }),
    ev("debate.round", { phase: "converge", round: 1, status: "started", open_challenges: 1 }),
    ev("debate.position", { member_id: "m1", round: 1, conclusion: "Solar leads", stances: [{ claim_id: "clm_1", stance: "endorse", reason: "sourced" }, { claim_id: "clm_2", stance: "object", reason: "unsourced" }] }),
    ev("debate.position", { member_id: "m2", round: 1, conclusion: "Unclear", stances: [{ claim_id: "clm_1", stance: "endorse" }, { claim_id: "clm_2", stance: "object" }] }),
    ev("debate.position", { member_id: "m3", round: 1, conclusion: "Wind rises", stances: [{ claim_id: "clm_1", stance: "endorse" }, { claim_id: "clm_2", stance: "endorse" }] }),
    ev("debate.consensus", {
      round: 1,
      claims: [{ claim_id: "clm_1", agreement: "agreed", endorse: ["m1", "m2", "m3"], object: [] }, { claim_id: "clm_2", agreement: "contested", endorse: ["m3"], object: ["m1", "m2"] }],
      signatures: [{ member_id: "m1", action: "sign", reason: "accurate" }, { member_id: "m2", action: "amend", reason: "soften wind", amendment: "Wind may grow" }, { member_id: "m3", action: "block", reason: "misrepresents clm_2" }],
      quorum_met: true, outcome: "revise",
    }),
    // The same exchanges also arrive as A2A messages; they must not show twice.
    ev("a2a.message", { message: { role: "agent", parts: [{ kind: "text", text: "challenge" }], metadata: { form: "challenge", from: { member_id: "m2" }, to: [{ member_id: "m1" }] } }, status: "ok" }),
  ]);
}

function helperRun(): RunState {
  seq = 0;
  return reduceAll({ ...emptyRun("r", MEMBERS), status: "running" }, [
    ev("member.joined", { member: { id: "h1", name: "Scout", role: "researcher", provider: "mock", model: "mock-2", ephemeral: true, parent_id: "m1" }, card: {} }, { actor: { kind: "agent", member_id: "m1", name: "Ada" } }),
    ev("member.live", { member_id: "h1", task: "Find 2025 data", tail: "Looking at the IEA tables" }, { ephemeral: true }),
    ev("member.joined", { member: { id: "h2", name: "Checker", role: "analyst", provider: "mock", model: "mock-2", ephemeral: true, parent_id: "m2" }, card: {} }),
    ev("member.left", { member_id: "h2", status: "completed", result_preview: "All three figures check out" }),
    ev("step.added", { step: { id: "s9", title: "Check the 2025 figures", assignee: "h1", depends_on: [] }, by: "m1" }),
    ev("context.saved", { member_id: "m1", task: "s1", raw_chars: 24000, sent_chars: 1500 }),
    ev("context.saved", { member_id: "m2", task: "s2", raw_chars: 6000, sent_chars: 500 }),
    ev("graph.updated", { nodes: 5, edges: 7 }, { ephemeral: true }),
  ]);
}

afterEach(() => {
  vi.restoreAllMocks();
  useRuns.setState({ runs: {} });
});

describe("run store: debate", () => {
  it("reduces claims, challenge threads, rounds, positions and the consensus", () => {
    const s = debateRun();
    const d = s.debate;
    expect(d.advocate).toEqual({ memberId: "m2", name: "Ben", pool: ["m1", "m2", "m3"] });
    expect(d.claimOrder).toEqual(["clm_1", "clm_2"]);
    // claim_status then consensus: the agreement is final.
    expect(d.claims.clm_1!.status).toBe("agreed");
    expect(d.claims.clm_2!.status).toBe("contested");
    expect(d.claims.clm_1!.evidence).toEqual([{ source: "[Source 1]", quote: "41 GW" }]);
    const c1 = d.challenges.chl_1!;
    expect(c1.responses.map((r) => r.unsupported)).toEqual([true, false]);
    expect(c1.resolution).toMatchObject({ outcome: "accepted", note: "Table 3 confirms it" });
    expect(challengeState(c1)).toBe("accepted");
    expect(challengeState(d.challenges.chl_2!)).toBe("open");
    expect(d.phase).toEqual({ phase: "converge", round: 1, status: "started", openChallenges: 1 });
    expect(d.phasesSeen).toEqual(["deliberate", "converge"]);
    expect(Object.keys(d.positions)).toEqual(["m1", "m2", "m3"]);
    expect(d.consensus).toMatchObject({ outcome: "revise", quorumMet: true });
    expect(d.consensus!.signatures.map((x) => x.action)).toEqual(["sign", "amend", "block"]);
    // Timeline: one row per debate event, none for claim_status or the duplicate A2A challenge.
    const kinds = s.order.map((id) => (s.items[id] as SimpleItem).type);
    expect(kinds).not.toContain("debate.claim_status");
    expect(kinds).not.toContain("a2a.message");
    expect(kinds.filter((k) => k.startsWith("debate."))).toHaveLength(13);
  });

  it("draws challenge edges challenger → target and answers back, instead of the A2A copy", () => {
    const s = debateRun();
    const edges = s.comms.filter((c) => c.kind === "challenge").map((c) => `${c.from.id}->${c.to.id}:${c.label}`);
    expect(edges).toEqual(["m2->m1:proof", "m1->m2:defend", "m1->m2:defend", "m2->m1:accepted", "m1->m3:counterexample"]);
    expect(s.comms.some((c) => c.kind === "a2a")).toBe(false);
  });

  it("takes agreement but not an outcome or quorum from a converge-phase consensus", () => {
    seq = 0;
    const s = reduceAll({ ...emptyRun("r", MEMBERS), status: "running" }, [
      ev("debate.claim", { claim_id: "clm_1", author_id: "m1", statement: "x", evidence: [], confidence: 0.5, status: "open" }),
      ev("debate.consensus", {
        phase: "converge", round: 1, quorum_met: null, outcome: null, signatures: [],
        claims: [{ claim_id: "clm_1", agreement: "contested", endorse: ["m1"], object: ["m2"] }],
      }),
    ]);
    expect(s.debate.claims.clm_1!.status).toBe("contested");
    expect(s.debate.consensus).toBeUndefined();
  });

  it("records a skipped advocate", () => {
    seq = 0;
    const s = reduceAll(emptyRun("r", MEMBERS), [ev("council.role_assigned", { skipped: true, reason: "fewer than two debaters" })]);
    expect(s.debate.advocate).toBeUndefined();
    expect(s.debate.advocateSkipped).toBe("fewer than two debaters");
  });
});

describe("run store: helpers, live tails and context", () => {
  it("adds helpers as members with their parent, tracks live tails, steps and context saved", () => {
    const s = helperRun();
    expect(s.members.map((m) => m.id)).toEqual(["m1", "m2", "m3", "h1", "h2"]);
    expect(s.members.find((m) => m.id === "h1")).toMatchObject({ ephemeral: true, parent_id: "m1", model: "mock-2" });
    expect(s.helpers.h1).toMatchObject({ parentId: "m1", status: "live" });
    expect(s.helpers.h2).toMatchObject({ status: "completed", resultPreview: "All three figures check out" });
    expect(s.live.h1).toMatchObject({ task: "Find 2025 data", tail: "Looking at the IEA tables" });
    expect(s.plan?.steps.map((x) => [x.id, x.assignee_member_id, x.status])).toEqual([["s9", "h1", "pending"]]);
    expect(contextCharsSaved(s.contextSaved)).toBe(28000);
    expect(s.contextSaved.tasks).toBe(2);
    expect(s.graph).toEqual({ nodes: 5, edges: 7 });
    expect(s.comms.filter((c) => c.kind === "spawn").map((c) => `${c.from.id}->${c.to.id}`)).toEqual(["m1->h1", "m2->h2", "h2->m2"]);
  });

  it("drops a finished helper's late live tail", () => {
    const s = reduceAll(helperRun(), [ev("member.live", { member_id: "h2", tail: "late" }, { ephemeral: true })]);
    expect(s.live.h2).toBeUndefined();
  });
});

describe("Debate view", () => {
  it("shows phases, claims with threads, the unsupported marker, positions and co-signing", () => {
    render(<DebatePanel runId="r" state={debateRun()} live />);
    expect(screen.getByRole("list", { name: "Debate phases" })).toBeInTheDocument();
    expect(screen.getByText("Converge").closest("li")).toHaveAttribute("aria-current", "step");
    expect(screen.getByText("Solar capacity grew 41 GW in 2024")).toBeInTheDocument();
    const thread = screen.getByRole("list", { name: "Challenges to claim #1" });
    expect(within(thread).getByText("demands proof")).toBeInTheDocument();
    expect(within(thread).getByText("unsupported")).toBeInTheDocument();
    expect(within(thread).getByText(/accepted the response/)).toBeInTheDocument();
    expect(within(thread).getByText("Table 3 confirms it")).toBeInTheDocument();
    const matrix = screen.getByRole("table");
    expect(within(matrix).getAllByRole("row")).toHaveLength(5); // header, three members, agreement
    expect(within(matrix).getAllByText("object")).toHaveLength(2);
    expect(within(matrix).getByText("agreed")).toBeInTheDocument();
    expect(screen.getByText("Sent back to revise")).toBeInTheDocument();
    const sigs = screen.getByRole("list", { name: "Signatures" });
    expect(within(sigs).getByText("amended")).toBeInTheDocument();
    expect(within(sigs).getByText("Wind may grow")).toBeInTheDocument();
    expect(within(sigs).getByText("blocked")).toBeInTheDocument();
  });

  it("shows what the debate cost, summed over its phases, without a timeline row", () => {
    const base = debateRun();
    const s = reduceAll(base, [
      ev("debate.cost", { phase: "deliberate", usd: 0.12, tokens: 9000, tasks: 5, light_models: { m1: "claude-haiku-5-5" } }),
      ev("debate.cost", { phase: "converge", usd: 0.05, tokens: 4000, tasks: 3, light_models: { m1: "claude-haiku-5-5" } }),
    ]);
    expect(s.debate.lightModels).toEqual({ m1: "claude-haiku-5-5" });
    expect(s.order.length).toBe(base.order.length);
    render(<DebatePanel runId="r" state={s} live />);
    expect(screen.getByText(/Debate \$0\.17 over 8 tasks/)).toBeInTheDocument();
  });

  it("marks the devil's advocate as a secret only the owner sees, wherever the member appears", () => {
    render(<DebatePanel runId="r" state={debateRun()} live />);
    const badges = screen.getAllByTitle(ADVOCATE_SECRET);
    expect(badges.length).toBeGreaterThan(3); // header, challenger, positions row, signature
    expect(badges[0]).toHaveTextContent(/Secret: only you can see this; the council does not know/);
  });

  it("messages a claim's author through the intervention flow, without mentioning the secret role", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(json({}, 202));
    render(<DebatePanel runId="r" state={debateRun()} live />);
    const thread = screen.getByRole("list", { name: "Challenges to claim #1" });
    await userEvent.click(within(thread).getByRole("button", { name: "Message Ben" }));
    const box = screen.getByLabelText("Message to Ben");
    expect((box as HTMLTextAreaElement).value).not.toMatch(/advocate/i);
    await userEvent.type(box, "show your source");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(String(url)).toBe("/api/v1/runs/r/interventions");
    const body = JSON.parse(String(init?.body));
    expect(body).toMatchObject({ kind: "message", target: { member_id: "m2" } });
    expect(body.text).toMatch(/show your source$/);
    expect(JSON.stringify(body)).not.toMatch(/advocate|stance/i);
  });

  it("has no axe violations", async () => {
    const { container } = render(<DebatePanel runId="r" state={debateRun()} live />);
    const results = await axe.run(container, { rules: { "color-contrast": { enabled: false } } });
    expect(results.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(", ")}`)).toEqual([]);
  });

  it("says so when there is no debate", () => {
    render(<DebatePanel runId="r" state={emptyRun("r", MEMBERS)} live={false} />);
    expect(screen.getByText("No debate in this run")).toBeInTheDocument();
  });
});

describe("timeline rows", () => {
  it("renders debate and helper events, with the advocate marked secret", () => {
    const d = debateRun();
    const h = helperRun();
    render(
      <MemoryRouter>
        {d.order.map((id) => <SimpleRow key={id} item={d.items[id] as SimpleItem} members={d.members} debate={d.debate} onOpenArtifact={() => {}} />)}
        {h.order.map((id) => <SimpleRow key={id} item={h.items[id] as SimpleItem} members={h.members} debate={h.debate} onOpenArtifact={() => {}} />)}
      </MemoryRouter>,
    );
    expect(screen.getByText(/is the devil's advocate/)).toBeInTheDocument();
    expect(screen.getAllByTitle(ADVOCATE_SECRET).length).toBeGreaterThan(0);
    expect(screen.getByText("That figure looks like 2023")).toBeInTheDocument();
    expect(screen.getByText(/Converge round 1 started/)).toBeInTheDocument();
    expect(screen.getByText(/1 signed, 1 amended, 1 blocked/)).toBeInTheDocument();
    expect(screen.getAllByText(/spawned helper/)).toHaveLength(2);
    expect(screen.getByText("All three figures check out")).toBeInTheDocument();
    expect(screen.getByText("Check the 2025 figures")).toBeInTheDocument();
  });
});

describe("helpers in the council", () => {
  it("lists helpers with their parent, state and live tail, and they can be held and messaged", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(json({}, 202));
    const s = helperRun();
    render(<CouncilRail runId="r" members={s.members} state={s} live onFocus={() => {}} focused={null} />);
    const helpers = screen.getByRole("heading", { name: "Helpers" }).parentElement!;
    const scout = within(helpers).getByText("Scout").closest("li")!;
    expect(within(scout).getByText("Helper of Ada")).toBeInTheDocument();
    expect(within(scout).getByText("working")).toBeInTheDocument();
    expect(within(scout).getByText(/Looking at the IEA tables/)).toBeInTheDocument();
    expect(within(scout).getByRole("button", { name: "Hold" })).toBeInTheDocument();
    const checker = within(helpers).getByText("Checker").closest("li")!;
    expect(within(checker).getByText("finished")).toBeInTheDocument();
    expect(within(checker).queryByRole("button", { name: "Hold" })).toBeNull();

    await userEvent.click(within(scout).getByRole("button", { name: "Message" }));
    await userEvent.type(within(scout).getByLabelText("Message to Scout"), "use 2025 data");
    await userEvent.click(within(scout).getByRole("button", { name: "Send" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    expect(JSON.parse(String(fetchMock.mock.calls[0]![1]?.body))).toEqual({ kind: "message", text: "use 2025 data", target: { member_id: "h1" } });
  });

  it("offers helpers as message targets in the intervention bar", async () => {
    const s = helperRun();
    render(<MemoryRouter><InterventionBar runId="r" members={s.members} disabled={false} /></MemoryRouter>);
    // jsdom matches no media query, so the bar takes its narrow form with the selects in a popover.
    await userEvent.click(screen.getByRole("button", { name: /change recipient or type/ }));
    expect(screen.getByRole("option", { name: "@Scout (helper)" })).toBeInTheDocument();
  });
});

describe("guardrails: debate, helpers and graph", () => {
  it("shows the backend defaults and bounds, and records overrides", async () => {
    const onChange = vi.fn();
    render(<GuardrailsEditor value={{}} onChange={onChange} />);
    expect(screen.getByLabelText("Parallel agents")).toHaveValue("8");
    expect(screen.getByLabelText("Deliberation rounds")).toHaveValue("2");
    expect(screen.getByLabelText("Consensus share")).toHaveValue("0.67");
    expect(screen.getByLabelText("Max live helpers")).toHaveValue("6");
    expect(screen.getByLabelText("Digest size")).toHaveValue("1500");

    await userEvent.click(screen.getByRole("switch", { name: "Debate" }));
    expect(onChange).toHaveBeenLastCalledWith({ debate: { enabled: false } });
    await userEvent.click(screen.getByRole("switch", { name: "Prompt caching" }));
    expect(onChange).toHaveBeenLastCalledWith({ graph: { prompt_cache: false } });

    const rounds = screen.getByLabelText("Deliberation rounds");
    await userEvent.clear(rounds);
    await userEvent.type(rounds, "7");
    expect(screen.getByText("Must be at most 6.")).toBeInTheDocument();
    const share = screen.getByLabelText("Consensus share");
    await userEvent.clear(share);
    await userEvent.type(share, "0.5");
    expect(screen.getByText("Must be at least 0.51.")).toBeInTheDocument();
    const depth = screen.getByLabelText("Max depth");
    await userEvent.clear(depth);
    await userEvent.type(depth, "3");
    expect(onChange).toHaveBeenLastCalledWith({ spawn: { max_depth: 3 } });
  });
});
