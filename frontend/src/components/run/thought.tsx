// What a thought card shows inside: the reply as prose, or, when it is JSON, a view built for its shape
// (a plan, a debate move, a ballot, an overseer review, or a readable tree), and the model's reasoning.

import { Braces, Brain, Check, ChevronRight, Copy, Link2, Vote } from "lucide-react";
import { useId, useMemo, useState, type ReactNode } from "react";

import { Badge, Button, IconButton, touchTarget } from "@/components/ui";
import { Markdown } from "@/components/ui/Markdown";
import { RoleAvatar } from "@/components/ui/role";
import { cn } from "@/lib/cn";
import { compact } from "@/lib/format";
import type { Member } from "@/lib/types";
import {
  classifyJson,
  extractJson,
  isObject,
  SHOWN_KEYS,
  str,
  strList,
  type JsonKind,
  type JsonObject,
  type JsonPart,
  type JsonValue,
} from "@/features/runs/thoughtJson";
import { toast } from "@/stores/toast";

type Tone = "neutral" | "info" | "success" | "warning" | "danger" | "accent";

// ------------------------------------------------------------------ small pieces
/** Long text clamped to a few lines, with "Show more". Short text renders as is. */
function Clamp({ text, lines = 3, className }: { text: string; lines?: 2 | 3; className?: string }) {
  const [open, setOpen] = useState(false);
  const id = useId();
  const long = text.length > (lines === 2 ? 140 : 220) || text.split("\n").length > lines;
  return (
    <div className={className}>
      <p id={id} className={cn("whitespace-pre-wrap break-words", long && !open && (lines === 2 ? "line-clamp-2" : "line-clamp-3"))}>{text}</p>
      {long && (
        <button type="button" aria-expanded={open} aria-controls={id} onClick={() => setOpen((o) => !o)}
          className={cn("min-h-6 text-xs font-medium text-accent hover:underline underline-offset-2", touchTarget)}>
          {open ? "Show less" : "Show more"}
        </button>
      )}
    </div>
  );
}

const safeUrl = (s: string) => (/^https?:\/\/\S+$/i.test(s) ? s : undefined);

function ExternalLink({ href, children }: { href: string; children: ReactNode }) {
  return <a href={href} target="_blank" rel="noopener noreferrer nofollow" className="text-accent underline break-all">{children}</a>;
}

/** A member mentioned by id, name or (when only one member has it) role. */
function resolveMember(members: Member[], key: string): Member | undefined {
  const k = key.trim().toLowerCase();
  if (!k) return undefined;
  const byRole = members.filter((m) => m.role.toLowerCase() === k);
  return members.find((m) => m.id.toLowerCase() === k) ?? members.find((m) => m.name.toLowerCase() === k)
    ?? (byRole.length === 1 ? byRole[0] : undefined);
}

/** Who a step or a challenge is for: the member's name and avatar when it matches one, else the text. */
function MemberChip({ value, members, prefix }: { value: string; members: Member[]; prefix: string }) {
  const m = resolveMember(members, value);
  return (
    <span className="inline-flex items-center gap-1 h-6 max-w-full rounded-full border border-border bg-surface px-2 text-xs text-fg-muted">
      <span className="sr-only">{prefix} </span>
      {m ? <><RoleAvatar role={m.role} size={16} label={m.role} /><span className="truncate font-medium text-fg">{m.name}</span></>
        : <span className="truncate font-mono">{value}</span>}
    </span>
  );
}

const Id = ({ children }: { children: ReactNode }) => <span className="font-mono text-2xs text-fg-subtle">{children}</span>;

/** Evidence as an agent cites it: `[{source, quote}]` or plain strings. URLs become links. */
function Evidence({ items }: { items: unknown }) {
  const list = Array.isArray(items) ? items : [];
  const rows = list.map((e) => (isObject(e) ? { source: str(e.source), quote: str(e.quote) } : { source: str(e), quote: "" })).filter((e) => e.source || e.quote);
  if (!rows.length) return null;
  return (
    <ul aria-label="Evidence" className="mt-1 flex flex-col gap-0.5 text-xs">
      {rows.map((e, i) => {
        const href = safeUrl(e.source);
        return (
          <li key={i} className="flex items-start gap-1 min-w-0">
            <Link2 className="size-3 mt-0.5 shrink-0 text-fg-subtle" aria-hidden />
            <span className="min-w-0 break-words">
              {href ? <ExternalLink href={href}>{e.source}</ExternalLink> : e.source && <span className="font-mono">{e.source}</span>}
              {e.quote && <q className="ml-1 text-fg-muted italic">{e.quote}</q>}
            </span>
          </li>
        );
      })}
    </ul>
  );
}

function Section({ title, count, children }: { title: string; count: number; children: ReactNode }) {
  if (!count) return null;
  return (
    <div className="flex flex-col gap-1.5">
      <p className="text-xs font-medium text-fg-subtle">{title} <span className="tabular">({count})</span></p>
      <ul aria-label={title} className="flex flex-col gap-2">{children}</ul>
    </div>
  );
}

const objects = (v: unknown): JsonObject[] => (Array.isArray(v) ? v.filter(isObject) : []);
const pick = (map: Record<string, Tone>, v: string, fallback: Tone = "neutral") => map[v.toLowerCase()] ?? fallback;

// ------------------------------------------------------------------ the purpose-built views
function PlanView({ v, members }: { v: JsonObject; members: Member[] }) {
  const steps = objects(v.steps);
  const titles = new Map(steps.map((s) => [str(s.id), str(s.title)]));
  return (
    <ol aria-label="Plan steps" className="flex flex-col gap-2">
      {steps.map((s, i) => {
        const id = str(s.id);
        const assignee = str(s.assignee) || str(s.assignee_member_id);
        const deps = strList(s.depends_on);
        const instructions = str(s.instructions) || str(s.description);
        return (
          <li key={id || i} className="flex gap-2">
            <span className="w-5 shrink-0 text-sm text-fg-subtle tabular">{i + 1}.</span>
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                <span className="text-sm font-medium text-fg">{str(s.title) || id || `Step ${i + 1}`}</span>
                {assignee && <MemberChip value={assignee} members={members} prefix="Assigned to" />}
                {id && <Id>{id}</Id>}
              </div>
              {deps.length > 0 && (
                <p className="mt-0.5 text-xs text-fg-subtle">
                  after {deps.map((d, k) => <span key={d}>{k > 0 && ", "}<span title={titles.get(d) || undefined} className="font-mono">{d}</span></span>)}
                </p>
              )}
              {instructions && <Clamp text={instructions} className="mt-0.5 text-sm text-fg-muted" />}
            </div>
          </li>
        );
      })}
    </ol>
  );
}

const DEMAND: Record<string, Tone> = { proof: "info", counterexample: "warning", clarification: "neutral" };
const OUTCOME: Record<string, Tone> = { accepted: "success", rejected: "danger" };

function ChallengesView({ v, members }: { v: JsonObject; members: Member[] }) {
  const challenges = objects(v.challenges);
  const resolutions = objects(v.resolutions);
  const claims = objects(v.claims);
  if (!challenges.length && !resolutions.length && !claims.length) {
    return <p className="text-sm text-fg-muted">No challenges, resolutions or new claims this round.</p>;
  }
  return (
    <div className="flex flex-col gap-3">
      <Section title="Challenges" count={challenges.length}>
        {challenges.map((c, i) => {
          const target = str(c.target) || str(c.claim_id);
          const demand = str(c.demand);
          return (
            <li key={i} className="flex flex-col gap-1">
              <div className="flex flex-wrap items-center gap-1.5">
                {target && <MemberChip value={target} members={members} prefix="Challenges" />}
                {demand && <Badge tone={pick(DEMAND, demand)}>demands {demand}</Badge>}
                {str(c.claim_id) && str(c.claim_id) !== target && <Id>{str(c.claim_id)}</Id>}
              </div>
              {str(c.objection) && <Clamp text={str(c.objection)} lines={2} className="text-sm text-fg" />}
            </li>
          );
        })}
      </Section>
      <Section title="Resolutions" count={resolutions.length}>
        {resolutions.map((r, i) => (
          <li key={i} className="flex flex-wrap items-center gap-1.5 text-sm">
            {str(r.challenge_id) && <Id>{str(r.challenge_id)}</Id>}
            {str(r.outcome) && <Badge tone={pick(OUTCOME, str(r.outcome))}>{str(r.outcome)}</Badge>}
            {str(r.note) && <span className="text-fg-muted">{str(r.note)}</span>}
          </li>
        ))}
      </Section>
      <Section title="New claims" count={claims.length}>
        {claims.map((c, i) => {
          const conf = typeof c.confidence === "number" ? c.confidence : undefined;
          return (
            <li key={i} className="flex flex-col">
              <div className="flex flex-wrap items-start gap-1.5">
                <span className="min-w-0 flex-1 text-sm text-fg">{str(c.statement)}</span>
                {conf != null && <Badge>{Math.round(conf <= 1 ? conf * 100 : conf)}% confident</Badge>}
              </div>
              <Evidence items={c.evidence} />
            </li>
          );
        })}
      </Section>
    </div>
  );
}

const RESPONSE: Record<string, Tone> = { defend: "info", concede: "warning", revise: "accent" };

function ResponsesView({ v }: { v: JsonObject }) {
  const responses = objects(v.responses);
  if (!responses.length) return <p className="text-sm text-fg-muted">No responses.</p>;
  return (
    <ul aria-label="Responses" className="flex flex-col gap-2">
      {responses.map((r, i) => (
        <li key={i} className="flex flex-col gap-1">
          <div className="flex flex-wrap items-center gap-1.5">
            {str(r.challenge_id) && <Id>{str(r.challenge_id)}</Id>}
            {str(r.stance) && <Badge tone={pick(RESPONSE, str(r.stance))}>{str(r.stance)}</Badge>}
          </div>
          {str(r.argument) && <Clamp text={str(r.argument)} lines={2} className="text-sm text-fg" />}
          <Evidence items={r.evidence} />
        </li>
      ))}
    </ul>
  );
}

const STANCE: Record<string, Tone> = { endorse: "success", object: "danger" };

function PositionView({ v }: { v: JsonObject }) {
  const stances = objects(v.stances);
  const alternative = str(v.alternative);
  return (
    <div className="flex flex-col gap-2">
      {str(v.conclusion) && (
        <div>
          <p className="text-xs font-medium text-fg-subtle">Conclusion</p>
          <Clamp text={str(v.conclusion)} className="text-sm text-fg" />
        </div>
      )}
      <Section title="Stances" count={stances.length}>
        {stances.map((s, i) => (
          <li key={i} className="flex flex-wrap items-center gap-1.5 text-sm">
            {str(s.claim_id) && <Id>{str(s.claim_id)}</Id>}
            {str(s.stance) && <Badge tone={pick(STANCE, str(s.stance))}>{str(s.stance)}</Badge>}
            {str(s.reason) && <span className="min-w-0 text-fg-muted">{str(s.reason)}</span>}
          </li>
        ))}
      </Section>
      {alternative && (
        <div>
          <p className="text-xs font-medium text-fg-subtle">Alternative</p>
          <Clamp text={alternative} lines={2} className="text-sm text-fg-muted" />
        </div>
      )}
    </div>
  );
}

const SIGN: Record<string, Tone> = { sign: "success", amend: "warning", block: "danger" };

function CosignView({ v }: { v: JsonObject }) {
  const action = str(v.action);
  const am = isObject(v.amendment) ? v.amendment : undefined;
  const passage = am ? str(am.passage) : "";
  const replacement = am ? str(am.replacement) : "";
  return (
    <div className="flex flex-col gap-1.5 text-sm">
      <div className="flex flex-wrap items-center gap-1.5">
        <Badge tone={pick(SIGN, action)}>{action}</Badge>
        {str(v.claim_id) && <Id>{str(v.claim_id)}</Id>}
        {str(v.reason) && <span className="min-w-0 text-fg-muted">{str(v.reason)}</span>}
      </div>
      {(passage || replacement) && (
        <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1 text-sm">
          {passage && <><dt className="text-xs text-fg-subtle pt-0.5">Replace</dt><dd><Clamp text={passage} lines={2} className="text-fg-muted line-through decoration-fg-subtle" /></dd></>}
          {replacement && <><dt className="text-xs text-fg-subtle pt-0.5">With</dt><dd><Clamp text={replacement} lines={2} className="text-fg" /></dd></>}
        </dl>
      )}
    </div>
  );
}

function AmendmentsView({ v }: { v: JsonObject }) {
  const endorse = strList(v.endorse);
  const reasons = isObject(v.reasons) ? v.reasons : {};
  if (!endorse.length) return <p className="text-sm text-fg-muted">Endorses none of the amendments.</p>;
  return (
    <Section title="Endorses" count={endorse.length}>
      {endorse.map((id) => (
        <li key={id} className="flex flex-wrap items-center gap-1.5 text-sm">
          <Badge tone="success">endorse</Badge><Id>{id}</Id>
          {str(reasons[id]) && <span className="min-w-0 text-fg-muted">{str(reasons[id])}</span>}
        </li>
      ))}
    </Section>
  );
}

function VoteView({ v }: { v: JsonObject }) {
  return (
    <p className="flex items-start gap-1.5 text-sm">
      <Vote className="size-4 mt-0.5 shrink-0 text-fg-subtle" aria-hidden />
      <span>Voted <strong className="font-semibold">{str(v.option)}</strong>{str(v.rationale) && <span className="text-fg-muted"> · {str(v.rationale)}</span>}</span>
    </p>
  );
}

const REVIEW: Record<string, Tone> = { pass: "success", annotate: "info", hold: "warning", block: "danger" };
const SEVERITY: Record<string, Tone> = { low: "neutral", medium: "warning", high: "danger" };

function OversightView({ v }: { v: JsonObject }) {
  const correction = str(v.correction);
  return (
    <div className="flex flex-col gap-1.5 text-sm">
      <div className="flex flex-wrap items-center gap-1.5">
        <Badge tone={pick(REVIEW, str(v.action))}>{str(v.action)}</Badge>
        {str(v.reason) && <Badge>{str(v.reason)}</Badge>}
        {str(v.severity) && <Badge tone={pick(SEVERITY, str(v.severity))}>{str(v.severity)} severity</Badge>}
        {str(v.note) && <span className="min-w-0 text-fg-muted">{str(v.note)}</span>}
      </div>
      {correction && correction !== "null" && <p><span className="text-xs text-fg-subtle">Correction </span>{correction}</p>}
    </div>
  );
}

// ------------------------------------------------------------------ the generic tree
const TREE_ITEMS = 100;
const TREE_DEPTH = 8;

function Scalar({ v }: { v: JsonValue }) {
  if (v === null) return <span className="font-mono text-xs text-fg-subtle italic">null</span>;
  if (typeof v === "string") {
    const href = safeUrl(v.trim());
    if (href) return <ExternalLink href={href}>{v}</ExternalLink>;
    return v ? <Clamp text={v} lines={3} className="text-fg" /> : <span className="font-mono text-xs text-fg-subtle">""</span>;
  }
  return <span className="font-mono text-xs text-fg">{String(v)}</span>;
}

function size(v: JsonValue[] | JsonObject) {
  const n = Array.isArray(v) ? v.length : Object.keys(v).length;
  return Array.isArray(v) ? `${n} item${n === 1 ? "" : "s"}` : `${n} field${n === 1 ? "" : "s"}`;
}

function TreeNode({ name, v, depth }: { name: string; v: JsonValue; depth: number }) {
  if (v !== null && typeof v === "object" && depth < TREE_DEPTH) {
    return (
      <li>
        <details open={depth < 1} className="group">
          <summary className={cn("flex min-h-6 cursor-pointer list-none items-center gap-1 text-sm [&::-webkit-details-marker]:hidden", touchTarget)}>
            <ChevronRight className="size-3.5 shrink-0 text-fg-subtle transition-transform group-open:rotate-90" aria-hidden />
            <span className="font-medium text-fg">{name}</span>
            <span className="text-xs text-fg-subtle">{size(v)}</span>
          </summary>
          <div className="ml-1.5 border-l border-border pl-3"><Tree v={v} depth={depth + 1} /></div>
        </details>
      </li>
    );
  }
  return (
    <li className="flex flex-wrap items-baseline gap-x-2 text-sm min-w-0">
      <span className="shrink-0 text-xs font-medium text-fg-subtle">{name}</span>
      <div className="min-w-0 flex-1">{v !== null && typeof v === "object" ? <Clamp text={JSON.stringify(v)} lines={2} className="font-mono text-xs" /> : <Scalar v={v} />}</div>
    </li>
  );
}

function Tree({ v, depth = 0 }: { v: JsonValue[] | JsonObject; depth?: number }) {
  const entries: [string, JsonValue][] = Array.isArray(v) ? v.map((x, i) => [`${i + 1}`, x]) : Object.entries(v);
  return (
    <ul className="flex flex-col gap-1 py-0.5">
      {entries.slice(0, TREE_ITEMS).map(([k, x]) => <TreeNode key={k} name={k} v={x} depth={depth} />)}
      {entries.length > TREE_ITEMS && <li className="text-xs text-fg-subtle">and {entries.length - TREE_ITEMS} more (see the raw JSON)</li>}
    </ul>
  );
}

// ------------------------------------------------------------------ the JSON block
function label(kind: JsonKind, v: JsonObject | JsonValue[]): string {
  const o = isObject(v) ? v : {};
  const n = (k: string) => objects(o[k]).length;
  switch (kind) {
    case "plan": return `Plan · ${n("steps")} step${n("steps") === 1 ? "" : "s"}`;
    case "challenges": return "Debate move";
    case "responses": return `Responses · ${n("responses")}`;
    case "position": return "Position";
    case "cosign": return "Co-sign";
    case "amendments": return "Amendment votes";
    case "vote": return "Ballot";
    case "oversight": return "Review";
    default: return "JSON";
  }
}

function View({ kind, v, members }: { kind: JsonKind; v: JsonObject | JsonValue[]; members: Member[] }) {
  if (!isObject(v)) return <Tree v={v} />;
  const rest = Object.fromEntries(Object.entries(v).filter(([k]) => !SHOWN_KEYS[kind].includes(k)));
  const main = kind === "plan" ? <PlanView v={v} members={members} />
    : kind === "challenges" ? <ChallengesView v={v} members={members} />
    : kind === "responses" ? <ResponsesView v={v} />
    : kind === "position" ? <PositionView v={v} />
    : kind === "cosign" ? <CosignView v={v} />
    : kind === "amendments" ? <AmendmentsView v={v} />
    : kind === "vote" ? <VoteView v={v} />
    : kind === "oversight" ? <OversightView v={v} />
    : null;
  if (!main) return <Tree v={v} />;
  return (
    <div className="flex flex-col gap-2">
      {main}
      {Object.keys(rest).length > 0 && (
        <div>
          <p className="text-xs font-medium text-fg-subtle">Other fields</p>
          <Tree v={rest} depth={1} />
        </div>
      )}
    </div>
  );
}

/** A JSON reply as a readable view, with "View raw" and copy so nothing is hidden. */
export function JsonBlock({ part, members = [] }: { part: JsonPart; members?: Member[] }) {
  const [raw, setRaw] = useState(false);
  const [copied, setCopied] = useState(false);
  const kind = classifyJson(part.value);
  const name = label(kind, part.value);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(part.raw);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      toast({ tone: "warning", title: "Copy failed", body: "Open the raw JSON and copy it manually." });
    }
  };
  return (
    <div role="group" aria-label={name} data-json-kind={kind} className="my-1 rounded-md border border-border bg-surface-2/40">
      <div className="flex items-center gap-1.5 border-b border-border pl-2.5 pr-1 py-0.5">
        <Braces className="size-3.5 shrink-0 text-fg-subtle" aria-hidden />
        <span className="text-xs font-medium text-fg-muted">{name}</span>
        <div className="ml-auto flex items-center gap-0.5">
          <Button size="sm" variant="ghost" aria-pressed={raw} onClick={() => setRaw((r) => !r)}>View raw</Button>
          <IconButton label={copied ? "Copied" : "Copy JSON"} icon={copied ? Check : Copy} onClick={() => void copy()} />
        </div>
      </div>
      <div className="px-2.5 py-2">
        {raw ? (
          <pre tabIndex={0} role="region" aria-label="Raw JSON"
            className="max-h-96 overflow-auto font-mono text-xs leading-5 text-fg whitespace-pre-wrap break-words">{part.raw}</pre>
        ) : <View kind={kind} v={part.value} members={members} />}
      </div>
    </div>
  );
}

/** A reply as it renders in a thought card: prose as Markdown, the JSON in it as a structured block. */
export function ThoughtText({ text, part: given, members = [], className }: {
  text: string;
  /** The JSON already found in `text`, when the caller has looked (null: there is none). */
  part?: JsonPart | null;
  members?: Member[];
  className?: string;
}) {
  const part = useMemo(() => (given !== undefined ? given : extractJson(text)), [given, text]);
  if (!part) return <Markdown className={className}>{text}</Markdown>;
  return (
    <div className="flex flex-col gap-1">
      {part.before && <Markdown className={className}>{part.before}</Markdown>}
      <JsonBlock part={part} members={members} />
      {part.after && <Markdown className={className}>{part.after}</Markdown>}
    </div>
  );
}

// ------------------------------------------------------------------ reasoning
/**
 * The model's chain of thought, above its answer. Open and live while only reasoning has arrived;
 * collapsed once the answer exists (the reader's own toggle wins after that).
 */
export function Reasoning({ text, tokens, streaming, answered, who }: {
  text: string; tokens?: number; streaming: boolean; answered: boolean; who: string;
}) {
  const [choice, setChoice] = useState<boolean | null>(null);
  const id = useId();
  const open = choice ?? !answered;
  const live = streaming && !answered;
  return (
    <div className="mt-2 rounded-md border-l-2 border-border-strong bg-surface-2/50 pl-2.5 pr-1.5 py-1" data-reasoning={open ? "open" : "closed"}>
      <button type="button" aria-expanded={open} aria-controls={open ? id : undefined} onClick={() => setChoice(!open)}
        aria-label={`Reasoning of ${who}${live ? ", thinking" : ""}${tokens ? `, ${compact(tokens)} tokens` : ""}`}
        className={cn("flex min-h-6 w-full items-center gap-1.5 text-left text-xs text-fg-muted hover:text-fg", touchTarget)}>
        <ChevronRight className={cn("size-3.5 shrink-0 transition-transform duration-fast", open && "rotate-90")} aria-hidden />
        <Brain className="size-3.5 shrink-0" aria-hidden />
        <span className="font-medium">Reasoning</span>
        {live && <span className="text-fg-subtle">thinking…</span>}
        {tokens != null && tokens > 0 && <span className="ml-auto tabular text-fg-subtle">{compact(tokens)} tok</span>}
      </button>
      {open && (
        <div id={id} role="region" aria-label={`Reasoning of ${who}`} tabIndex={0}
          className={cn("mt-1 max-h-72 overflow-auto pb-1 pr-1", live && "caret")}>
          <Markdown className="text-sm text-fg-muted">{text}</Markdown>
        </div>
      )}
    </div>
  );
}
