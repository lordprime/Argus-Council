// Run replay (terminal runs): re-feeds the stored event log through the same reducer the live
// stream uses (stores/run.ts) into a separate replay state, keyed `replay:<runId>` in the runs
// store, so every run view renders the run as it unfolded. The real run state is never touched.

import { useQuery } from "@tanstack/react-query";
import { History, Pause, Play, SkipBack, SkipForward, X } from "lucide-react";
import { useCallback, useEffect, useId, useRef, useState, type KeyboardEvent } from "react";

import { Button, IconButton } from "@/components/ui";
import { api, errorMessage, V1 } from "@/lib/api";
import { cn } from "@/lib/cn";
import type { Member, RunEvent } from "@/lib/types";
import { emptyRun, reduceAll, useRuns, type RunState } from "@/stores/run";

export const SPEEDS = [1, 2, 4, 8] as const;
export type Speed = (typeof SPEEDS)[number];

export const replayKey = (runId: string) => `replay:${runId}`;

/** The runs-store key the run views should read: the replay state while replaying, else the run. */
export function useReplayView(runId: string): string {
  return useRuns((st) => (st.runs[replayKey(runId)] ? replayKey(runId) : runId));
}

/** State after the first `pos` events. `runId` stays the real run id so views link correctly. */
export function replayStateAt(runId: string, members: Member[], events: RunEvent[], pos: number): RunState {
  return reduceAll(emptyRun(runId, members), events.slice(0, Math.max(0, Math.min(pos, events.length))));
}

/** Real-time gap before event `i`, compressed into [40ms, 1s] and divided by the speed. */
export function stepDelay(events: RunEvent[], i: number, speed: number): number {
  const prev = events[i - 1];
  const cur = events[i];
  const gap = prev && cur ? Date.parse(cur.ts) - Date.parse(prev.ts) : 0;
  return Math.min(1000, Math.max(40, Number.isFinite(gap) ? gap : 0)) / speed;
}

export async function fetchAllEvents(runId: string): Promise<RunEvent[]> {
  const out: RunEvent[] = [];
  let after = 0;
  for (let page = 0; page < 100; page++) {
    const r = await api.get<{ events: RunEvent[]; has_more: boolean }>(
      `${V1}/runs/${encodeURIComponent(runId)}/events?after_seq=${after}&limit=2000`,
    );
    out.push(...r.events);
    if (!r.has_more || !r.events.length) break;
    after = r.events[r.events.length - 1]!.seq;
  }
  return out;
}

export function prefersReducedMotion(): boolean {
  if (typeof document !== "undefined" && document.documentElement.dataset.motion === "reduced") return true;
  try {
    return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  } catch {
    return false;
  }
}

const offset = (events: RunEvent[], pos: number) => {
  if (!events.length || pos === 0) return 0;
  return Math.max(0, (Date.parse(events[pos - 1]!.ts) - Date.parse(events[0]!.ts)) / 1000);
};
const mmss = (s: number) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;

function ReplayControls({ runId, members, events, onExit }: { runId: string; members: Member[]; events: RunEvent[]; onExit: () => void }) {
  const key = replayKey(runId);
  const total = events.length;
  const [pos, setPos] = useState(0);
  const [playing, setPlaying] = useState(() => !prefersReducedMotion() && total > 0);
  const [speed, setSpeed] = useState<Speed>(1);
  const rendered = useRef(-1);
  const sliderId = useId();

  // Write the replay state for `pos`: one incremental reducer step when moving forward by one, else a rebuild.
  useEffect(() => {
    const store = useRuns.getState();
    if (rendered.current >= 0 && pos === rendered.current + 1 && store.runs[key]) {
      store.apply(key, [events[pos - 1]!]);
    } else {
      const next = replayStateAt(runId, members, events, pos);
      useRuns.setState((st) => ({ runs: { ...st.runs, [key]: next } }));
    }
    rendered.current = pos;
  }, [pos, events, key, runId, members]);

  useEffect(() => () => useRuns.getState().drop(key), [key]);

  useEffect(() => {
    if (!playing) return;
    if (pos >= total) {
      setPlaying(false);
      return;
    }
    const t = window.setTimeout(() => setPos((p) => Math.min(total, p + 1)), stepDelay(events, pos, speed));
    return () => window.clearTimeout(t);
  }, [playing, pos, total, speed, events]);

  const step = useCallback((d: number) => {
    setPlaying(false);
    setPos((p) => Math.max(0, Math.min(total, p + d)));
  }, [total]);
  const toggle = useCallback(() => {
    if (pos >= total) setPos(0);
    setPlaying((p) => !p || pos >= total);
  }, [pos, total]);

  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    const tag = (e.target as HTMLElement).tagName;
    const onControl = tag === "BUTTON" || tag === "INPUT" || tag === "SELECT";
    if (e.key === "k" || (e.key === " " && !onControl)) {
      e.preventDefault();
      toggle();
    } else if (e.key === "," || e.key === "j" || (e.key === "ArrowLeft" && !onControl)) {
      e.preventDefault();
      step(-1);
    } else if (e.key === "." || e.key === "l" || (e.key === "ArrowRight" && !onControl)) {
      e.preventDefault();
      step(1);
    }
  };

  const current = pos > 0 ? events[pos - 1] : undefined;
  const valueText = current ? `Event ${pos} of ${total}: ${current.type} (seq ${current.seq})` : `Start, 0 of ${total} events`;
  return (
    // Shortcuts (k / , / .) apply while focus is inside the replay controls.
    // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions
    <div role="region" aria-label="Run replay" onKeyDown={onKey}
      className="flex flex-wrap items-center gap-2 px-3 py-2 border-b border-border bg-accent-subtle/40">
      <span className="text-xs font-semibold text-accent inline-flex items-center gap-1"><History className="size-4" aria-hidden /> Replay</span>
      <IconButton label="Step back" icon={SkipBack} onClick={() => step(-1)} disabled={pos <= 0} />
      <IconButton label={playing ? "Pause replay" : "Play replay"} icon={playing ? Pause : Play} onClick={toggle}
        disabled={total === 0} aria-pressed={playing} />
      <IconButton label="Step forward" icon={SkipForward} onClick={() => step(1)} disabled={pos >= total} />
      <label htmlFor={sliderId} className="sr-only">Replay position</label>
      <input id={sliderId} type="range" min={0} max={total} step={1} value={pos} aria-valuetext={valueText}
        onChange={(e) => { setPlaying(false); setPos(Number(e.target.value)); }}
        className="flex-1 min-w-32 accent-accent" />
      <span className="text-xs tabular text-fg-muted whitespace-nowrap" data-testid="replay-position">
        {pos}/{total} · seq {current?.seq ?? 0} · {mmss(offset(events, pos))}/{mmss(offset(events, total))}
      </span>
      <div role="group" aria-label="Replay speed" className="inline-flex rounded-md bg-surface-2 p-0.5">
        {SPEEDS.map((s) => (
          <button key={s} type="button" aria-pressed={speed === s} onClick={() => setSpeed(s)}
            className={cn("px-2 h-7 rounded text-xs tabular", speed === s ? "bg-surface shadow-1 font-semibold" : "text-fg-muted hover:text-fg")}>
            {s}×
          </button>
        ))}
      </div>
      <IconButton label="Exit replay" icon={X} onClick={onExit} />
    </div>
  );
}

/** Replay entry point for a terminal run: a button, then the controls once the event log is loaded. */
export function ReplayBar({ runId, members }: { runId: string; members: Member[] }) {
  const [active, setActive] = useState(false);
  const events = useQuery({
    queryKey: ["runEvents", runId],
    queryFn: () => fetchAllEvents(runId),
    enabled: active,
    staleTime: Infinity,
  });
  const exit = useCallback(() => {
    setActive(false);
    useRuns.getState().drop(replayKey(runId));
  }, [runId]);

  if (!active) {
    return (
      <div className="flex items-center gap-2 px-3 py-1.5 border-b border-border bg-surface">
        <Button size="sm" variant="ghost" icon={History} onClick={() => setActive(true)}>Replay run</Button>
        <span className="text-xs text-fg-subtle hidden sm:inline">Watch every view rebuild event by event.</span>
      </div>
    );
  }
  if (events.error) {
    return (
      <div className="flex items-center gap-2 px-3 py-1.5 border-b border-border bg-danger-subtle text-sm" role="alert">
        Could not load the event log: {errorMessage(events.error)}
        <Button size="sm" variant="ghost" onClick={() => void events.refetch()}>Retry</Button>
        <IconButton label="Exit replay" icon={X} onClick={exit} />
      </div>
    );
  }
  if (!events.data) {
    return <div className="px-3 py-2 border-b border-border text-xs text-fg-subtle" role="status">Loading the event log…</div>;
  }
  return <ReplayControls runId={runId} members={members} events={events.data} onExit={exit} />;
}
