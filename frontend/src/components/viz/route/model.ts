// Route milestones: plan steps form the trail from Start to the Deliverable (summit); intercepts,
// approvals, votes, sub-councils, protocol calls and corrections become pins along it.

import { isTerminal, type RunState, type TimelineItem } from "@/stores/run";

import { actorNode, ms, orderedItems, type CommEventLike, type RunWithViz, type SubCouncilLike } from "../heatmap/comms";

export type MilestoneKind = "start" | "step" | "intercept" | "approval" | "vote" | "subcouncil" | "protocol" | "correction" | "summit";
export type Tone = "done" | "active" | "pending" | "alert" | "warn" | "info";

export interface Milestone {
  id: string;
  kind: MilestoneKind;
  /** epoch ms, 0 when not reached yet */
  t: number;
  /** position along the trail: 0 = start, i + 1 = plan step i, steps + 1 = summit */
  anchor: number;
  title: string;
  who?: string;
  detail?: string;
  tone: Tone;
  itemId?: string;
  nodeId?: string;
  hash?: string;
}

export interface RouteModel {
  milestones: Milestone[];
  steps: number;
  done: number;
  /** current position along the trail, in anchor units */
  current: number;
  reached: boolean;
}

const DONE = new Set(["completed", "complete", "done", "succeeded", "success", "skipped"]);
const ACTIVE = new Set(["running", "in_progress", "active", "started"]);

const clip = (s: string | undefined, n = 140) => (s && s.length > n ? `${s.slice(0, n - 1)}…` : s);
const str = (v: unknown) => (typeof v === "string" ? v : undefined);

export function buildRoute(run: RunState): RouteModel {
  const vr = run as RunWithViz;
  const steps = run.plan?.steps ?? [];
  const n = steps.length;
  const items = orderedItems(run);
  const memberName = (id?: string) => (id ? run.members.find((m) => m.id === id)?.name : undefined);

  const stepIdx = new Map(steps.map((s, i) => [s.id, i]));
  const stepFirst = new Map<string, TimelineItem>();
  for (const it of items) {
    const sid = "stepId" in it ? it.stepId : undefined;
    if (sid && !stepFirst.has(sid)) stepFirst.set(sid, it);
  }
  const times = items.map((i) => ms(i.ts)).filter((t) => t > 0);
  const t0 = times.length ? Math.min(...times) : 0;
  const t1 = times.length ? Math.max(...times) : 0;
  const stepStarts = steps.map((s) => ms(stepFirst.get(s.id)?.ts));

  /** Where along the trail an event at time t (optionally tied to a step) sits. */
  const anchorFor = (t: number, stepId?: string): number => {
    const si = stepId !== undefined ? stepIdx.get(stepId) : undefined;
    if (si !== undefined) return si + 0.6;
    if (stepStarts.some((x) => x > 0)) {
      let k = 0;
      stepStarts.forEach((x, i) => { if (x > 0 && x <= t) k = i + 1; });
      return Math.min(n + 0.5, k + 0.5);
    }
    if (t1 > t0 && t > 0) return ((t - t0) / (t1 - t0)) * (n + 1) * 0.95;
    return 0.5;
  };

  const out: Milestone[] = [];
  out.push({ id: "route:start", kind: "start", t: t0, anchor: 0, title: "Start", detail: "Run started", tone: "done" });

  steps.forEach((s, i) => {
    const first = stepFirst.get(s.id);
    const status = s.status?.toLowerCase() ?? "pending";
    out.push({
      id: `route:step:${s.id}`, kind: "step", t: ms(first?.ts), anchor: i + 1, title: s.title,
      who: memberName(s.assignee_member_id), detail: clip(s.summary) ?? status.replace("_", " "),
      tone: DONE.has(status) ? "done" : ACTIVE.has(status) ? "active" : status === "failed" || status === "error" ? "alert" : "pending",
      itemId: first?.id, nodeId: s.assignee_member_id,
    });
  });

  const comms: CommEventLike[] = vr.comms ?? [];
  const commsHave = (k: string) => comms.some((c) => c.kind === k);

  for (const it of items) {
    const t = ms(it.ts);
    const sid = "stepId" in it ? it.stepId : undefined;
    const who = "actor" in it ? actorNode(it.actor, run).label : undefined;
    if (it.kind === "thought") {
      for (const ic of it.intercepts) {
        out.push({ id: `route:ic:${ic.id}`, kind: "intercept", t, anchor: anchorFor(t, sid), title: `Intercept · ${ic.severity}`,
          who: actorNode(ic.actor, run).label, detail: clip(ic.reason), tone: "alert", itemId: it.id });
        if (ic.correction) {
          out.push({ id: `route:corr:${ic.id}`, kind: "correction", t: t + 1, anchor: anchorFor(t, sid) + 0.1, title: "Correction",
            who: actorNode(ic.actor, run).label, detail: clip(ic.correction), tone: "warn", itemId: it.id });
        }
      }
    } else if (it.kind === "approval") {
      out.push({ id: `route:apr:${it.id}`, kind: "approval", t, anchor: anchorFor(t), title: `Approval · ${it.status}`, who,
        detail: clip(it.summary), tone: it.status === "approved" ? "done" : it.status === "pending" ? "warn" : "alert", itemId: it.id });
    } else if (it.kind === "vote") {
      out.push({ id: `route:vote:${it.id}`, kind: "vote", t, anchor: anchorFor(t), title: "Vote", detail: clip(`${it.motion}${it.result ? ` → ${it.result}` : ""}`),
        who: `${it.casts.length} vote${it.casts.length === 1 ? "" : "s"}`, tone: "info", itemId: it.id });
    } else if (it.kind === "user" && it.type === "correction.issued") {
      out.push({ id: `route:corr:${it.id}`, kind: "correction", t, anchor: anchorFor(t, sid), title: "Correction", who,
        detail: clip(str(it.data.text) ?? str(it.data.correction) ?? str(it.data.note)), tone: "warn", itemId: it.id });
    } else if (it.kind === "system" && it.type === "intercept.raised") {
      out.push({ id: `route:ic:${it.id}`, kind: "intercept", t, anchor: anchorFor(t, sid), title: `Intercept · ${str(it.data.severity) ?? "alert"}`, who,
        detail: clip(str(it.data.reason)), tone: "alert", itemId: it.id });
    } else if (it.kind === "system" && it.type.startsWith("subcouncil") && !commsHave("subcouncil")) {
      out.push({ id: `route:sub:${it.id}`, kind: "subcouncil", t, anchor: anchorFor(t, sid), title: "Sub-council", who,
        detail: clip(str(it.data.task) ?? it.type), tone: "info", itemId: it.id });
    } else if (it.kind === "tool" && (it.actor.kind === "remote" || it.actor.protocol || it.source === "remote") && !commsHave("protocol")) {
      out.push({ id: `route:proto:${it.id}`, kind: "protocol", t, anchor: anchorFor(t, sid), title: `${it.actor.protocol ?? "Protocol"} call`, who,
        detail: clip(`${it.tool} · ${it.status}`), tone: it.status === "ok" ? "info" : "alert", itemId: it.id });
    }
  }

  for (const c of comms) {
    if (c.kind !== "protocol" && c.kind !== "subcouncil") continue;
    const t = ms(c.ts);
    const item = run.items[c.id];
    const sid = item && "stepId" in item ? item.stepId : undefined;
    if (c.kind === "protocol") {
      out.push({ id: `route:proto:${c.id}`, kind: "protocol", t, anchor: anchorFor(t, sid),
        title: `${c.ledger?.protocol ?? "Protocol"} · ${c.ledger?.method ?? c.label ?? "call"}`, who: `${c.from.label} → ${c.to.label}`,
        detail: c.ledger ? `${c.ledger.latencyMs} ms` : clip(c.label), hash: c.ledger?.hash,
        tone: c.status && c.status !== "ok" ? "alert" : "info", itemId: c.id, nodeId: c.to.id });
    } else {
      out.push({ id: `route:sub:${c.id}`, kind: "subcouncil", t, anchor: anchorFor(t, sid), title: "Sub-council",
        who: `${c.from.label} → ${c.to.label}`, detail: clip(c.label), tone: "info", itemId: c.id, nodeId: c.childRunId ?? c.to.id });
    }
  }

  if (!commsHave("subcouncil") && !out.some((m) => m.kind === "subcouncil") && vr.subcouncils) {
    const subs: SubCouncilLike[] = Array.isArray(vr.subcouncils) ? vr.subcouncils : Object.values(vr.subcouncils);
    for (const sc of subs) {
      const t = ms(sc.startedTs);
      out.push({ id: `route:sub:${sc.id}`, kind: "subcouncil", t, anchor: anchorFor(t), title: `Sub-council · ${sc.status}`,
        detail: clip(sc.task), tone: "info", nodeId: sc.id });
    }
  }

  const done = steps.filter((s) => DONE.has(s.status?.toLowerCase())).length;
  const running = steps.some((s) => ACTIVE.has(s.status?.toLowerCase()));
  const reached = run.status === "completed";
  const current = reached ? n + 1 : Math.min(n + 0.5, done + (running ? 0.5 : 0));
  out.push({
    id: "route:summit", kind: "summit", t: isTerminal(run.status) ? t1 : 0, anchor: n + 1, title: "Deliverable",
    detail: reached ? "Delivered" : isTerminal(run.status) ? `Run ${run.status}` : "Not reached yet",
    tone: reached ? "done" : isTerminal(run.status) ? "alert" : "pending",
  });

  // Chronological; unreached milestones (t = 0) follow in trail order, summit last.
  const rank = (m: Milestone) => (m.kind === "summit" ? 2 : m.t > 0 || m.kind === "start" ? 0 : 1);
  out.sort((a, b) => rank(a) - rank(b) || (rank(a) === 0 ? a.t - b.t : a.anchor - b.anchor) || a.anchor - b.anchor);
  return { milestones: out, steps: n, done, current, reached };
}
