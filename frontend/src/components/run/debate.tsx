// The council's debate in the run workspace (docs/design/parallel-council.md, sections 2 and 3):
// the Debate view (phases, claims with their challenge threads, positions, co-signing), the timeline
// rows for debate and helper events, and the member markers (secret devil's advocate, helper of X)
// used wherever members are shown.

import {
  Check,
  EyeOff,
  Flag,
  ListPlus,
  MessageSquareReply,
  Minus,
  Scale,
  Send,
  Signature as SignatureIcon,
  Swords,
  UserMinus,
  UserPlus,
  X,
} from "lucide-react";
import { useId, useState, type ReactNode } from "react";

import { Badge, Button, EmptyState } from "@/components/ui";
import { api, errorMessage, V1 } from "@/lib/api";
import { cn } from "@/lib/cn";
import { compact } from "@/lib/format";
import type { Member } from "@/lib/types";
import {
  DEBATE_PHASES,
  challengeState,
  hasDebate,
  openChallenges,
  type Challenge,
  type Claim,
  type DebatePhase,
  type DebateState,
  type Evidence,
  type HelperInfo,
} from "@/stores/debate";
import { contextCharsSaved, useRun, type RunState, type SimpleItem } from "@/stores/run";
import { toast } from "@/stores/toast";

type Tone = "neutral" | "info" | "success" | "warning" | "danger" | "accent";

// ------------------------------------------------------------------ member markers
/** Shown with the devil's advocate badge: the pick is private to the run's owner. */
export const ADVOCATE_SECRET = "Secret: only you can see this; the council does not know";

/**
 * The devil's advocate marker. The council is never told who it is, so the badge carries a private
 * marker and nothing it shows is ever sent back to the server.
 */
export function AdvocateBadge({ short }: { short?: boolean }) {
  return (
    <span title={ADVOCATE_SECRET}
      className="inline-flex items-center gap-1 rounded-sm px-1.5 h-5 text-2xs font-medium whitespace-nowrap bg-warning-subtle text-warning">
      <EyeOff className="size-3" aria-hidden />
      {short ? "Advocate" : "Devil's advocate"}
      <span className="sr-only"> ({ADVOCATE_SECRET})</span>
    </span>
  );
}

const HELPER_STATE: Record<HelperInfo["status"], { label: string; tone: Tone }> = {
  live: { label: "working", tone: "info" },
  completed: { label: "finished", tone: "success" },
  failed: { label: "failed", tone: "danger" },
  cancelled: { label: "cancelled", tone: "neutral" },
};

/** "Helper of X" plus its live or finished state. */
export function HelperBadge({ parent, info }: { parent?: string; info?: HelperInfo }) {
  const st = info ? HELPER_STATE[info.status] : undefined;
  return (
    <span className="inline-flex flex-wrap items-center gap-1">
      <Badge tone="accent"><UserPlus className="size-3" aria-hidden />Helper{parent ? ` of ${parent}` : ""}</Badge>
      {st && <Badge tone={st.tone}>{st.label}</Badge>}
    </span>
  );
}

const nameOf = (members: Member[], id?: string) => members.find((m) => m.id === id)?.name ?? (id ? id : "A member");

/** A member's name with the markers that apply to it (devil's advocate, helper). */
export function MemberLabel({ id, members, debate, className }: { id?: string; members: Member[]; debate?: DebateState; className?: string }) {
  const m = members.find((x) => x.id === id);
  return (
    <span className={cn("inline-flex flex-wrap items-center gap-1", className)}>
      <span className="font-medium text-fg">{nameOf(members, id)}</span>
      {id && debate?.advocate?.memberId === id && <AdvocateBadge short />}
      {m?.ephemeral && <Badge tone="accent">helper</Badge>}
    </span>
  );
}

/** "Working now": a member's current task and the tail of its latest thought. */
export function LiveTailLine({ tail }: { tail?: { task?: string; tail: string } }) {
  if (!tail || (!tail.tail && !tail.task)) return null;
  return (
    <p className="mt-1.5 rounded-md bg-surface-2 px-2 py-1 text-xs text-fg-muted">
      <span className="font-medium text-fg">Working now</span>
      {tail.task && <span>: {tail.task}</span>}
      {tail.tail && <span className="block line-clamp-2 italic">…{tail.tail.slice(-300)}</span>}
    </p>
  );
}

// ------------------------------------------------------------------ message a member
/**
 * "Message this member": the ordinary intervention (kind=message to one member). The draft names
 * only the claim or challenge it is about; it never mentions a secret stance.
 */
export function MessageMember({ runId, member, about, label }: { runId: string; member?: Member; about?: string; label?: string }) {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const id = useId();
  if (!member || runId.startsWith("replay:")) return null;
  const start = () => {
    setText(about ? `${about}: ` : "");
    setOpen(true);
  };
  const send = async () => {
    if (!text.trim()) return;
    setBusy(true);
    try {
      await api.post(`${V1}/runs/${runId}/interventions`, { kind: "message", text, target: { member_id: member.id } });
      toast({ tone: "success", title: `Message sent to ${member.name}` });
      setOpen(false);
      setText("");
    } catch (err) {
      toast({ tone: "danger", title: "Could not send", body: errorMessage(err) });
    } finally {
      setBusy(false);
    }
  };
  if (!open) {
    return (
      <Button size="sm" variant="ghost" icon={MessageSquareReply} onClick={start} aria-expanded={false}>
        {label ?? `Message ${member.name}`}
      </Button>
    );
  }
  return (
    <form aria-label={`Message ${member.name}`} className="mt-1 flex w-full flex-col gap-1.5"
      onSubmit={(e) => { e.preventDefault(); void send(); }}>
      <label htmlFor={id} className="text-xs font-medium">Message to {member.name}</label>
      <textarea id={id} autoFocus rows={2} value={text} onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => { if ((e.metaKey || e.ctrlKey) && e.key === "Enter") void send(); if (e.key === "Escape") setOpen(false); }}
        className="w-full rounded-md border border-border-strong bg-surface p-2 text-sm" />
      <div className="flex justify-end gap-2">
        <Button size="sm" type="button" variant="ghost" onClick={() => setOpen(false)}>Cancel</Button>
        <Button size="sm" type="submit" variant="primary" icon={Send} loading={busy} disabled={!text.trim()}>Send</Button>
      </div>
    </form>
  );
}

// ------------------------------------------------------------------ chips
export const CLAIM_TONE: Record<string, Tone> = {
  open: "neutral", supported: "success", disputed: "warning", conceded: "neutral", revised: "info",
  agreed: "success", contested: "warning", rejected: "danger",
};

const DEMAND_LABEL: Record<string, string> = { proof: "demands proof", counterexample: "demands a counterexample", clarification: "asks for clarification" };
const STANCE_LABEL: Record<string, string> = { defend: "defended", concede: "conceded", revise: "revised" };
const SIGN_LABEL: Record<string, { label: string; tone: Tone }> = {
  sign: { label: "signed", tone: "success" }, amend: { label: "amended", tone: "info" }, block: { label: "blocked", tone: "danger" },
};
const OUTCOME_LABEL: Record<string, { label: string; tone: Tone }> = {
  signed: { label: "Signed by the council", tone: "success" },
  revise: { label: "Sent back to revise", tone: "warning" },
  unsigned: { label: "Not signed", tone: "danger" },
};
const PHASE_LABEL: Record<DebatePhase, string> = { deliberate: "Deliberate", converge: "Converge", compile: "Compile", cosign: "Co-sign" };
const CHALLENGE_STATE: Record<ReturnType<typeof challengeState>, { label: string; tone: Tone }> = {
  open: { label: "open", tone: "warning" },
  answered: { label: "answered", tone: "info" },
  unsupported: { label: "open", tone: "warning" },
  accepted: { label: "response accepted", tone: "success" },
  rejected: { label: "response rejected", tone: "danger" },
};

export function ClaimStatusChip({ status }: { status: string }) {
  return <Badge tone={CLAIM_TONE[status] ?? "neutral"}>{status}</Badge>;
}

function UnsupportedMarker() {
  return (
    <Badge tone="danger">
      <Flag className="size-3" aria-hidden />unsupported
      <span className="sr-only">: proof was demanded and no evidence was given</span>
    </Badge>
  );
}

function EvidenceList({ evidence }: { evidence: Evidence[] }) {
  if (!evidence.length) return null;
  return (
    <ul aria-label="Evidence" className="mt-1 flex flex-col gap-0.5 text-xs text-fg-muted">
      {evidence.map((e, i) => (
        <li key={i} className="break-words">
          {/^https?:\/\//.test(e.source)
            ? <a href={e.source} target="_blank" rel="noopener noreferrer nofollow" className="text-accent underline break-all">{e.source}</a>
            : <span className="font-mono">{e.source}</span>}
          {e.quote && <q className="ml-1 italic">{e.quote}</q>}
        </li>
      ))}
    </ul>
  );
}

// ------------------------------------------------------------------ the Debate view
function PhaseIndicator({ debate }: { debate: DebateState }) {
  const cur = debate.phase;
  const curIdx = cur ? DEBATE_PHASES.indexOf(cur.phase) : -1;
  const open = cur?.openChallenges ?? openChallenges(debate);
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
      <ol aria-label="Debate phases" className="flex flex-wrap items-center gap-1 text-sm">
        {DEBATE_PHASES.map((p, i) => {
          const done = i < curIdx || (i === curIdx && cur?.status === "finished");
          const now = i === curIdx && cur?.status === "started";
          return (
            <li key={p} aria-current={now ? "step" : undefined} className="flex items-center gap-1">
              {i > 0 && <span aria-hidden className="text-fg-subtle">→</span>}
              <span className={cn("inline-flex items-center gap-1 rounded-md px-2 h-7",
                now ? "bg-accent-subtle text-accent font-semibold ring-1 ring-inset ring-accent" : done ? "text-fg" : "text-fg-subtle")}>
                {done && <Check className="size-3.5" aria-hidden />}
                {PHASE_LABEL[p]}
                <span className="sr-only">{now ? " (in progress)" : done ? " (done)" : " (not started)"}</span>
              </span>
            </li>
          );
        })}
      </ol>
      <p className="text-xs text-fg-muted tabular" role="status">
        {cur ? `${PHASE_LABEL[cur.phase]} round ${cur.round}${cur.status === "finished" ? " finished" : ""}` : "Members debate while they work"}
        {` · ${open} open challenge${open === 1 ? "" : "s"}`}
      </p>
    </div>
  );
}

function ChallengeThread({ c, runId, members, debate, live }: { c: Challenge; runId: string; members: Member[]; debate: DebateState; live: boolean }) {
  const st = CHALLENGE_STATE[challengeState(c)];
  const claim = c.claimId ? debate.claims[c.claimId] : undefined;
  const challenger = members.find((m) => m.id === c.challengerId);
  const target = members.find((m) => m.id === (claim?.authorId ?? c.targetId));
  const about = claim ? `About challenge ${c.id} on claim #${claim.n}` : `About challenge ${c.id}`;
  return (
    <li className="border-l-2 border-warning/60 pl-3 py-1">
      <p className="flex flex-wrap items-center gap-x-1.5 gap-y-1 text-xs text-fg-muted">
        <Swords className="size-3.5 text-warning" aria-hidden />
        <MemberLabel id={c.challengerId} members={members} debate={debate} />
        <span>challenged</span>
        <MemberLabel id={c.targetId} members={members} debate={debate} />
        <Badge tone="warning">{DEMAND_LABEL[c.demand] ?? c.demand}</Badge>
        <Badge tone={st.tone}>{st.label}</Badge>
        {c.round != null && <span className="tabular">round {c.round}</span>}
      </p>
      <p className="mt-0.5 text-sm whitespace-pre-wrap break-words">{c.objection}</p>
      {c.responses.length > 0 && (
        <ol aria-label="Responses" className="mt-1.5 flex flex-col gap-1.5">
          {c.responses.map((r, i) => (
            <li key={i} className="ml-3 border-l-2 border-border-strong pl-3">
              <p className="flex flex-wrap items-center gap-x-1.5 gap-y-1 text-xs text-fg-muted">
                <MessageSquareReply className="size-3.5" aria-hidden />
                <MemberLabel id={r.responderId} members={members} debate={debate} />
                <Badge tone={r.stance === "concede" ? "neutral" : r.stance === "revise" ? "info" : "accent"}>{STANCE_LABEL[r.stance] ?? r.stance}</Badge>
                {r.unsupported && <UnsupportedMarker />}
              </p>
              <p className="mt-0.5 text-sm whitespace-pre-wrap break-words">{r.argument}</p>
              <EvidenceList evidence={r.evidence} />
            </li>
          ))}
        </ol>
      )}
      {c.resolution && (
        <p className="ml-3 mt-1.5 flex flex-wrap items-center gap-1.5 text-xs">
          {c.resolution.outcome === "rejected" ? <X className="size-3.5 text-danger" aria-hidden /> : <Check className="size-3.5 text-success" aria-hidden />}
          <span className="text-fg-muted">{nameOf(members, c.challengerId)} {c.resolution.outcome === "rejected" ? "rejected the response" : "accepted the response"}</span>
          {c.resolution.note && <span className="text-fg">{c.resolution.note}</span>}
        </p>
      )}
      {live && (
        <div className="mt-1 flex flex-wrap gap-1">
          <MessageMember runId={runId} member={challenger} about={about} />
          {target && target.id !== challenger?.id && <MessageMember runId={runId} member={target} about={about} />}
        </div>
      )}
    </li>
  );
}

function ClaimCard({ claim, runId, members, debate, live }: { claim: Claim; runId: string; members: Member[]; debate: DebateState; live: boolean }) {
  const threads = debate.challengeOrder.map((id) => debate.challenges[id]!).filter((c) => c.claimId === claim.id);
  const author = members.find((m) => m.id === claim.authorId);
  return (
    <li id={`claim-${claim.id}`} className="rounded-lg border border-border bg-surface p-3">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-fg-muted">
        <span className="font-mono text-fg-subtle">#{claim.n}</span>
        <ClaimStatusChip status={claim.status} />
        <MemberLabel id={claim.authorId} members={members} debate={debate} />
        {claim.confidence != null && <span className="tabular">confidence {Math.round(claim.confidence * (claim.confidence <= 1 ? 100 : 1))}%</span>}
        {threads.length > 0 && <span className="ml-auto">{threads.length} challenge{threads.length === 1 ? "" : "s"}</span>}
      </div>
      <p className="mt-1 text-sm whitespace-pre-wrap break-words">{claim.statement}</p>
      <EvidenceList evidence={claim.evidence} />
      {live && <div className="mt-1"><MessageMember runId={runId} member={author} about={`About claim #${claim.n}`} /></div>}
      {threads.length > 0 && (
        <ul aria-label={`Challenges to claim #${claim.n}`} className="mt-2 flex flex-col gap-2">
          {threads.map((c) => <ChallengeThread key={c.id} c={c} runId={runId} members={members} debate={debate} live={live} />)}
        </ul>
      )}
    </li>
  );
}

function StanceCell({ stance, reason }: { stance?: "endorse" | "object"; reason?: string }) {
  if (!stance) return <td className="px-2 py-1.5 text-center text-fg-subtle"><Minus className="inline size-3.5" aria-hidden /><span className="sr-only">no stance</span></td>;
  const endorse = stance === "endorse";
  return (
    <td className="px-2 py-1.5 text-center" title={reason}>
      <span className={cn("inline-flex items-center gap-1 text-xs font-medium", endorse ? "text-success" : "text-danger")}>
        {endorse ? <Check className="size-3.5" aria-hidden /> : <X className="size-3.5" aria-hidden />}
        {endorse ? "endorse" : "object"}
      </span>
      {reason && <span className="sr-only">: {reason}</span>}
    </td>
  );
}

function PositionsMatrix({ debate, members }: { debate: DebateState; members: Member[] }) {
  const rows = Object.values(debate.positions).sort((a, b) =>
    members.findIndex((m) => m.id === a.memberId) - members.findIndex((m) => m.id === b.memberId));
  if (!rows.length) return null;
  const referenced = new Set([...rows.flatMap((p) => p.stances.map((s) => s.claimId)), ...(debate.consensus?.claims.map((c) => c.claimId) ?? [])]);
  const keys = [...debate.claimOrder.filter((id) => referenced.has(id)), ...[...referenced].filter((id) => !debate.claims[id])];
  const agreement = new Map(debate.consensus?.claims.map((c) => [c.claimId, c.agreement]) ?? []);
  const col = (id: string) => (debate.claims[id] ? `#${debate.claims[id]!.n}` : id);
  return (
    <section aria-labelledby="positions-h">
      <h3 id="positions-h" className="text-sm font-semibold mb-1.5 flex items-center gap-1.5"><Scale className="size-4" aria-hidden />Positions</h3>
      <div tabIndex={0} role="region" aria-label="Positions on key claims" className="overflow-x-auto rounded-lg border border-border">
        <table className="w-full text-sm">
          <caption className="sr-only">Each member's stance on each key claim, and the agreement computed from them</caption>
          <thead className="bg-surface-2 text-xs text-fg-muted">
            <tr>
              <th scope="col" className="px-2 py-1.5 text-left font-medium">Member</th>
              {keys.map((id) => (
                <th key={id} scope="col" className="px-2 py-1.5 font-medium" title={debate.claims[id]?.statement}>
                  <span className="font-mono">{col(id)}</span>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((p) => (
              <tr key={p.memberId} className="border-t border-border">
                <th scope="row" className="px-2 py-1.5 text-left font-normal"><MemberLabel id={p.memberId} members={members} debate={debate} /></th>
                {keys.map((id) => {
                  const s = p.stances.find((x) => x.claimId === id);
                  return <StanceCell key={id} stance={s?.stance} reason={s?.reason} />;
                })}
              </tr>
            ))}
            {agreement.size > 0 && (
              <tr className="border-t-2 border-border-strong bg-surface-2">
                <th scope="row" className="px-2 py-1.5 text-left text-xs font-medium">Agreement</th>
                {keys.map((id) => (
                  <td key={id} className="px-2 py-1.5 text-center">{agreement.get(id) ? <ClaimStatusChip status={agreement.get(id)!} /> : null}</td>
                ))}
              </tr>
            )}
          </tbody>
        </table>
      </div>
      <details className="mt-1.5 text-sm">
        <summary className="cursor-pointer text-fg-muted">Conclusions ({rows.length})</summary>
        <ul className="mt-1 flex flex-col gap-1.5">
          {rows.map((p) => (
            <li key={p.memberId}>
              <MemberLabel id={p.memberId} members={members} debate={debate} />
              <span className="text-fg-subtle text-xs"> · round {p.round}</span>
              <p className="whitespace-pre-wrap break-words">{p.conclusion}</p>
            </li>
          ))}
        </ul>
      </details>
    </section>
  );
}

function ConsensusCard({ debate, members }: { debate: DebateState; members: Member[] }) {
  const c = debate.consensus;
  if (!c) return null;
  const outcome = OUTCOME_LABEL[c.outcome] ?? { label: c.outcome, tone: "neutral" as Tone };
  return (
    <section aria-labelledby="consensus-h" className="rounded-lg border border-border bg-surface p-3">
      <h3 id="consensus-h" className="text-sm font-semibold flex flex-wrap items-center gap-2">
        <SignatureIcon className="size-4" aria-hidden />Conclusion
        <Badge tone={outcome.tone}>{outcome.label}</Badge>
        <Badge tone={c.quorumMet ? "success" : "warning"}>{c.quorumMet ? "quorum met" : "no quorum"}</Badge>
        <span className="text-xs font-normal text-fg-subtle tabular">round {c.round}</span>
      </h3>
      {c.signatures.length > 0 && (
        <ul aria-label="Signatures" className="mt-2 flex flex-col gap-1.5">
          {c.signatures.map((s) => {
            const a = SIGN_LABEL[s.action] ?? { label: s.action, tone: "neutral" as Tone };
            return (
              <li key={s.memberId} className="text-sm">
                <span className="inline-flex flex-wrap items-center gap-1.5">
                  <MemberLabel id={s.memberId} members={members} debate={debate} />
                  <Badge tone={a.tone}>{a.label}</Badge>
                  {s.reason && <span className="text-fg-muted">{s.reason}</span>}
                </span>
                {s.amendment && <blockquote className="mt-0.5 ml-2 border-l-2 border-info pl-2 text-xs whitespace-pre-wrap">{s.amendment}</blockquote>}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

/** The Debate view: what the council claims, how it was challenged, where it stands, and who signed. */
export function DebatePanel({ runId, state, live }: { runId: string; state: RunState; live: boolean }) {
  const debate = state.debate;
  const members = state.members;
  if (!hasDebate(debate)) {
    return (
      <div className="p-4">
        <EmptyState icon={Swords} title={live ? "No debate yet" : "No debate in this run"}
          body="Claims, challenges, positions and the co-signed conclusion appear here as members argue." />
      </div>
    );
  }
  const loose = debate.challengeOrder.map((id) => debate.challenges[id]!).filter((c) => !c.claimId || !debate.claims[c.claimId]);
  const saved = contextCharsSaved(state.contextSaved);
  const spent = (debate.costs ?? []).reduce((a, c) => ({ usd: a.usd + c.usd, tasks: a.tasks + c.tasks }), { usd: 0, tasks: 0 });
  return (
    <div className="flex flex-col gap-4 p-3 md:p-4 max-w-5xl">
      <div className="flex flex-col gap-2">
        <PhaseIndicator debate={debate} />
        <p className="text-xs text-fg-muted flex flex-wrap items-center gap-1.5">
          {debate.advocate ? (
            <>Devil's advocate: <MemberLabel id={debate.advocate.memberId} members={members} />
              <AdvocateBadge /></>
          ) : debate.advocateSkipped ? <>No devil's advocate: {debate.advocateSkipped}</> : null}
          {(saved > 0 || spent.tasks > 0) && (
            <span className="ml-auto tabular">
              {spent.tasks > 0 && <>Debate ${spent.usd.toFixed(2)} over {spent.tasks} tasks{saved > 0 ? " · " : ""}</>}
              {saved > 0 && <>{compact(saved)} context chars saved</>}
            </span>
          )}
        </p>
      </div>
      <ConsensusCard debate={debate} members={members} />
      <PositionsMatrix debate={debate} members={members} />
      <section aria-labelledby="claims-h">
        <h3 id="claims-h" className="text-sm font-semibold mb-1.5">Claims ({debate.claimOrder.length})</h3>
        {debate.claimOrder.length ? (
          <ol className="flex flex-col gap-2">
            {debate.claimOrder.map((id) => <ClaimCard key={id} claim={debate.claims[id]!} runId={runId} members={members} debate={debate} live={live} />)}
          </ol>
        ) : <p className="text-sm text-fg-subtle">No claims yet.</p>}
      </section>
      {loose.length > 0 && (
        <section aria-labelledby="loose-h">
          <h3 id="loose-h" className="text-sm font-semibold mb-1.5">Challenges to members' work</h3>
          <ul className="flex flex-col gap-2 rounded-lg border border-border bg-surface p-3">
            {loose.map((c) => <ChallengeThread key={c.id} c={c} runId={runId} members={members} debate={debate} live={live} />)}
          </ul>
        </section>
      )}
    </div>
  );
}

// ------------------------------------------------------------------ timeline rows
function Line({ icon: Icon, tone, children }: { icon: typeof Swords; tone?: string; children: ReactNode }) {
  return (
    <div className={cn("flex items-start gap-2 text-sm px-1", tone ?? "text-fg-muted")}>
      <Icon className="size-4 mt-0.5 shrink-0" aria-hidden />
      <div className="min-w-0 flex-1 flex flex-wrap items-center gap-x-1.5 gap-y-1">{children}</div>
    </div>
  );
}

const clip = (t: unknown, n = 240) => {
  const s = typeof t === "string" ? t : "";
  return s.length > n ? `${s.slice(0, n - 1)}…` : s;
};

/** Timeline row for a debate event (council.role_assigned, debate.*). */
export function DebateRow({ item, members, debate }: { item: SimpleItem; members: Member[]; debate?: DebateState }) {
  const d = item.data as Record<string, any>; // eslint-disable-line @typescript-eslint/no-explicit-any
  const who = (id?: string) => <MemberLabel id={id} members={members} debate={debate} />;
  switch (item.type) {
    case "council.role_assigned":
      if (d.skipped) return <Line icon={EyeOff}>No devil's advocate this run: {d.reason ?? "skipped"}</Line>;
      return (
        <Line icon={EyeOff}>
          <span className="font-medium text-fg">{d.name ?? nameOf(members, d.member_id)}</span> is the devil's advocate
          {Array.isArray(d.pool) && d.pool.length > 0 && <span>(picked at random from {d.pool.length} debaters)</span>}
          <AdvocateBadge />
        </Line>
      );
    case "debate.claim": {
      const c = debate?.claims[d.claim_id];
      return (
        <Line icon={Flag}>
          {who(d.author_id)} claimed{c && <span className="font-mono text-fg-subtle">#{c.n}</span>}
          <ClaimStatusChip status={c?.status ?? d.status ?? "open"} />
          <span className="basis-full text-fg">{clip(d.statement)}</span>
        </Line>
      );
    }
    case "debate.challenge": {
      const c = d.claim_id ? debate?.claims[d.claim_id] : undefined;
      return (
        <Line icon={Swords} tone="text-fg-muted">
          {who(d.challenger_id)} challenged {who(d.target_id)}{c && <span>on claim <span className="font-mono">#{c.n}</span></span>}
          <Badge tone="warning">{DEMAND_LABEL[d.demand] ?? d.demand}</Badge>
          <span className="basis-full text-fg">{clip(d.objection)}</span>
        </Line>
      );
    }
    case "debate.response":
      return (
        <Line icon={MessageSquareReply}>
          {who(d.responder_id)} {STANCE_LABEL[d.stance] ?? d.stance}
          {d.unsupported && <UnsupportedMarker />}
          <span className="basis-full text-fg">{clip(d.argument)}</span>
        </Line>
      );
    case "debate.resolution": {
      const ch = debate?.challenges[d.challenge_id];
      return (
        <Line icon={d.outcome === "rejected" ? X : Check}>
          {who(ch?.challengerId)} {d.outcome === "rejected" ? "rejected the response" : "accepted the response"}
          {d.note && <span className="text-fg">{clip(d.note)}</span>}
        </Line>
      );
    }
    case "debate.round":
      return (
        <Line icon={Scale}>
          {PHASE_LABEL[d.phase as DebatePhase] ?? d.phase} round {d.round} {d.status === "finished" ? "finished" : "started"}
          {d.open_challenges != null && <span>· {d.open_challenges} open challenge{d.open_challenges === 1 ? "" : "s"}</span>}
        </Line>
      );
    case "debate.position": {
      const st = (d.stances ?? []) as { stance: string }[];
      const yes = st.filter((x) => x.stance === "endorse").length;
      return (
        <Line icon={Scale}>
          {who(d.member_id)} took a position: endorses {yes}, objects to {st.length - yes}
          {d.conclusion && <span className="basis-full text-fg">{clip(d.conclusion)}</span>}
        </Line>
      );
    }
    case "debate.consensus": {
      if (d.phase === "converge") {
        const claims = (d.claims ?? []) as { agreement: string }[];
        const k = (a: string) => claims.filter((c) => c.agreement === a).length;
        return (
          <Line icon={Scale}>
            Converging, round {d.round ?? 1}: {k("agreed")} agreed, {k("contested")} contested, {k("rejected")} rejected
          </Line>
        );
      }
      const sig = (d.signatures ?? []) as { action: string }[];
      const n = (a: string) => sig.filter((x) => x.action === a).length;
      const o = OUTCOME_LABEL[d.outcome] ?? { label: d.outcome, tone: "neutral" as Tone };
      return (
        <Line icon={SignatureIcon}>
          Co-signing: <Badge tone={o.tone}>{o.label}</Badge>
          <span>{n("sign")} signed, {n("amend")} amended, {n("block")} blocked{d.quorum_met ? " · quorum met" : " · no quorum"}</span>
        </Line>
      );
    }
    default:
      return <Line icon={Scale}>{item.type}</Line>;
  }
}

/** Timeline row for helpers joining and leaving, and steps added while the run works. */
export function CouncilRow({ item, members }: { item: SimpleItem; members: Member[] }) {
  const d = item.data as Record<string, any>; // eslint-disable-line @typescript-eslint/no-explicit-any
  switch (item.type) {
    case "member.joined": {
      const m = d.member ?? {};
      return (
        <Line icon={UserPlus}>
          <span className="font-medium text-fg">{nameOf(members, m.parent_id)}</span> spawned helper
          <span className="font-medium text-fg">{m.name}</span>
          <span className="text-fg-subtle">({[m.role, m.model].filter(Boolean).join(", ")})</span>
        </Line>
      );
    }
    case "member.left":
      return (
        <Line icon={UserMinus} tone={d.status === "failed" ? "text-danger" : undefined}>
          Helper <span className="font-medium text-fg">{nameOf(members, d.member_id)}</span> {d.status === "completed" ? "finished" : d.status}
          {d.result_preview && <span className="basis-full text-fg line-clamp-2">{clip(d.result_preview)}</span>}
        </Line>
      );
    case "step.added": {
      const st = d.step ?? {};
      return (
        <Line icon={ListPlus}>
          <span className="font-medium text-fg">{nameOf(members, d.by ?? item.actor.member_id)}</span> added a step:
          <span className="text-fg">{st.title}</span>
          {st.assignee && <span>→ {nameOf(members, st.assignee)}</span>}
        </Line>
      );
    }
    default:
      return <Line icon={UserPlus}>{item.type}</Line>;
  }
}

/** Advocate id for a run, for cards that show one member (thought cards). */
export function useAdvocate(runId: string): string | undefined {
  return useRun(runId, (s) => s.debate?.advocate?.memberId);
}
