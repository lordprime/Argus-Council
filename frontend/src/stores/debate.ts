// The council's debate, helpers and context accounting, reduced from run events
// (docs/design/parallel-council.md, "Contract shared by the workstreams"). Pure functions: each one
// takes the previous value and returns a new one, so earlier run states are never mutated.

export type ClaimStatus = "open" | "supported" | "disputed" | "conceded" | "revised" | "agreed" | "contested" | "rejected";
export type DebatePhase = "deliberate" | "converge" | "compile" | "cosign";
export const DEBATE_PHASES: DebatePhase[] = ["deliberate", "converge", "compile", "cosign"];

export interface Evidence { source: string; quote?: string }

export interface Claim {
  id: string;
  /** 1-based number in the order claims were made, for short references ("#2"). */
  n: number;
  authorId: string;
  statement: string;
  evidence: Evidence[];
  confidence?: number;
  status: ClaimStatus;
  seq: number;
  ts: string;
}

export interface ChallengeResponse {
  responderId: string;
  stance: string;
  argument: string;
  evidence: Evidence[];
  /** A defence against a proof demand that brought no evidence: the challenge stays open. */
  unsupported: boolean;
  seq: number;
  ts: string;
}

export interface Challenge {
  id: string;
  claimId?: string;
  challengerId: string;
  targetId: string;
  objection: string;
  demand: string;
  round?: number;
  responses: ChallengeResponse[];
  resolution?: { outcome: string; note?: string; ts: string };
  seq: number;
  ts: string;
}

export interface PositionStance { claimId: string; stance: "endorse" | "object"; reason?: string }
export interface Position { memberId: string; round: number; conclusion: string; stances: PositionStance[]; ts: string }

export interface Signature { memberId: string; action: "sign" | "amend" | "block"; reason?: string; amendment?: string }
export interface Consensus {
  round: number;
  claims: { claimId: string; agreement: string; endorse: string[]; object: string[] }[];
  signatures: Signature[];
  quorumMet: boolean;
  outcome: string;
  ts: string;
}

export interface DebateState {
  /**
   * The devil's advocate. A secret only the run's owner sees: the council is never told, so nothing
   * derived from it may be sent back to the server (messages, corrections) or shared.
   */
  advocate?: { memberId: string; name?: string; pool: string[] };
  /** Why no advocate was picked (fewer than two debaters, or the setting is off). */
  advocateSkipped?: string;
  phase?: { phase: DebatePhase; round: number; status: "started" | "finished"; openChallenges?: number };
  /** Phases that have started, in order. */
  phasesSeen: DebatePhase[];
  claims: Record<string, Claim>;
  claimOrder: string[];
  challenges: Record<string, Challenge>;
  challengeOrder: string[];
  /** Latest position per member. */
  positions: Record<string, Position>;
  consensus?: Consensus;
  /** What each phase of the debate cost (`debate.cost`), and the light model each seat used. */
  costs: { phase: string; usd: number; tokens: number; tasks: number }[];
  lightModels: Record<string, string>;
}

export interface HelperInfo {
  parentId?: string;
  status: "live" | "completed" | "failed" | "cancelled";
  resultPreview?: string;
  joinedTs: string;
  leftTs?: string;
}

/** A member's current task and the tail of its latest thought (`member.live`, not stored server-side). */
export interface LiveTail { task?: string; tail: string; ts: string }

export interface ContextSavings { raw: number; sent: number; tasks: number }

export const emptyDebate = (): DebateState => ({
  phasesSeen: [], claims: {}, claimOrder: [], challenges: {}, challengeOrder: [], positions: {}, costs: [], lightModels: {},
});

type D = Record<string, any>; // eslint-disable-line @typescript-eslint/no-explicit-any

const evidenceOf = (v: unknown): Evidence[] =>
  Array.isArray(v)
    ? v.map((e) => (typeof e === "string" ? { source: e } : { source: String(e?.source ?? ""), quote: e?.quote || undefined })).filter((e) => e.source || e.quote)
    : [];

/** Apply one debate event (`council.role_assigned`, `debate.*`); returns the new debate state. */
export function reduceDebate(prev: DebateState, type: string, d: D, at: { seq: number; ts: string }): DebateState {
  switch (type) {
    case "council.role_assigned":
      if (d.skipped) return { ...prev, advocate: undefined, advocateSkipped: String(d.reason ?? "skipped") };
      if (!d.member_id) return prev;
      return { ...prev, advocateSkipped: undefined, advocate: { memberId: String(d.member_id), name: d.name, pool: Array.isArray(d.pool) ? d.pool : [] } };
    case "debate.claim": {
      const id = String(d.claim_id);
      const old = prev.claims[id];
      const claim: Claim = {
        id, n: old?.n ?? prev.claimOrder.length + 1, authorId: String(d.author_id ?? ""), statement: String(d.statement ?? ""),
        evidence: evidenceOf(d.evidence), confidence: typeof d.confidence === "number" ? d.confidence : undefined,
        status: (d.status as ClaimStatus) ?? old?.status ?? "open", seq: old?.seq ?? at.seq, ts: old?.ts ?? at.ts,
      };
      return { ...prev, claims: { ...prev.claims, [id]: claim }, claimOrder: old ? prev.claimOrder : [...prev.claimOrder, id] };
    }
    case "debate.claim_status": {
      const c = prev.claims[d.claim_id];
      if (!c || !d.status) return prev;
      return { ...prev, claims: { ...prev.claims, [c.id]: { ...c, status: d.status } } };
    }
    case "debate.challenge": {
      const id = String(d.challenge_id);
      const old = prev.challenges[id];
      const ch: Challenge = {
        id, claimId: d.claim_id || undefined, challengerId: String(d.challenger_id ?? ""), targetId: String(d.target_id ?? ""),
        objection: String(d.objection ?? ""), demand: String(d.demand ?? "clarification"), round: d.round ?? undefined,
        responses: old?.responses ?? [], resolution: old?.resolution, seq: old?.seq ?? at.seq, ts: old?.ts ?? at.ts,
      };
      return { ...prev, challenges: { ...prev.challenges, [id]: ch }, challengeOrder: old ? prev.challengeOrder : [...prev.challengeOrder, id] };
    }
    case "debate.response": {
      const ch = prev.challenges[d.challenge_id];
      if (!ch) return prev;
      const evidence = evidenceOf(d.evidence);
      const stance = String(d.stance ?? "defend");
      // The server marks a defence of a proof demand without evidence; derive it for older events.
      const unsupported = typeof d.unsupported === "boolean" ? d.unsupported : stance === "defend" && ch.demand === "proof" && !evidence.length;
      const r: ChallengeResponse = {
        responderId: String(d.responder_id ?? ch.targetId), stance, argument: String(d.argument ?? ""), evidence, unsupported, seq: at.seq, ts: at.ts,
      };
      return { ...prev, challenges: { ...prev.challenges, [ch.id]: { ...ch, responses: [...ch.responses, r] } } };
    }
    case "debate.resolution": {
      const ch = prev.challenges[d.challenge_id];
      if (!ch) return prev;
      return { ...prev, challenges: { ...prev.challenges, [ch.id]: { ...ch, resolution: { outcome: String(d.outcome ?? ""), note: d.note || undefined, ts: at.ts } } } };
    }
    case "debate.round": {
      const phase = d.phase as DebatePhase;
      if (!DEBATE_PHASES.includes(phase)) return prev;
      return {
        ...prev,
        phase: { phase, round: Number(d.round ?? 1), status: d.status === "finished" ? "finished" : "started", openChallenges: d.open_challenges ?? undefined },
        phasesSeen: prev.phasesSeen.includes(phase) ? prev.phasesSeen : [...prev.phasesSeen, phase],
      };
    }
    case "debate.cost":
      return {
        ...prev,
        costs: [...(prev.costs ?? []), { phase: String(d.phase ?? ""), usd: Number(d.usd ?? 0), tokens: Number(d.tokens ?? 0), tasks: Number(d.tasks ?? 0) }],
        lightModels: { ...(prev.lightModels ?? {}), ...((d.light_models ?? {}) as Record<string, string>) },
      };
    case "debate.position": {
      const memberId = String(d.member_id ?? "");
      if (!memberId) return prev;
      const stances: PositionStance[] = (Array.isArray(d.stances) ? d.stances : []).map((x: D) => ({
        claimId: String(x.claim_id), stance: x.stance === "object" ? "object" : "endorse", reason: x.reason || undefined,
      }));
      return { ...prev, positions: { ...prev.positions, [memberId]: { memberId, round: Number(d.round ?? 1), conclusion: String(d.conclusion ?? ""), stances, ts: at.ts } } };
    }
    case "debate.consensus": {
      const claims = (Array.isArray(d.claims) ? d.claims : []).map((c: D) => ({
        claimId: String(c.claim_id), agreement: String(c.agreement ?? ""), endorse: c.endorse ?? [], object: c.object ?? [],
      }));
      const signatures: Signature[] = (Array.isArray(d.signatures) ? d.signatures : []).map((x: D) => ({
        memberId: String(x.member_id), action: x.action, reason: x.reason || undefined, amendment: x.amendment || undefined,
      }));
      // Agreement is final for the claims it covers.
      let nextClaims = prev.claims;
      for (const c of claims) {
        const old = nextClaims[c.claimId];
        if (old && ["agreed", "contested", "rejected"].includes(c.agreement) && old.status !== c.agreement) {
          nextClaims = { ...nextClaims, [c.claimId]: { ...old, status: c.agreement as ClaimStatus } };
        }
      }
      // Converging only settles agreement on claims; the outcome and quorum come from co-signing.
      if (d.phase === "converge") return { ...prev, claims: nextClaims };
      return {
        ...prev, claims: nextClaims,
        consensus: { round: Number(d.round ?? 1), claims, signatures, quorumMet: Boolean(d.quorum_met), outcome: String(d.outcome ?? ""), ts: at.ts },
      };
    }
    default:
      return prev;
  }
}

/** A challenge's state, from its responses and resolution. */
export function challengeState(c: Challenge): "open" | "answered" | "unsupported" | "accepted" | "rejected" {
  if (c.resolution) return c.resolution.outcome === "rejected" ? "rejected" : "accepted";
  const last = c.responses[c.responses.length - 1];
  if (!last) return "open";
  return last.unsupported ? "unsupported" : "answered";
}

/** Challenges still waiting for an answer or a resolution. */
export const openChallenges = (d: DebateState) =>
  d.challengeOrder.filter((id) => {
    const st = challengeState(d.challenges[id]!);
    return st === "open" || st === "unsupported";
  }).length;

export const hasDebate = (d: DebateState | undefined) =>
  Boolean(d && (d.advocate || d.advocateSkipped || d.phase || d.claimOrder.length || d.challengeOrder.length || d.consensus));
