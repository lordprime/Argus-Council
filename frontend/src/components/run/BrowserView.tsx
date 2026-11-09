// The Browser view of a run: one card per member that opened a browser. While the run is live and the
// session open it shows the member's browser as it is now (watch-only: nothing here sends input to the
// page); otherwise it replays the screenshots the run kept, in order, with the actions between them.

import { Globe } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { Badge, EmptyState } from "@/components/ui";
import { followFrames } from "@/lib/frames";
import { artifactImage } from "@/lib/images";
import type { BrowserTrail, RunState } from "@/stores/run";

const LIVE_ACTIONS = 5;

/**
 * Follow the member's frames while `enabled`; the stream is aborted on unmount, when the session closes
 * and when it is replaced (a new session id reconnects, and the old session's last frame is dropped).
 */
function useFrames(runId: string, memberId: string, sessionId: string, enabled: boolean): string | null {
  const [frame, setFrame] = useState<{ key: string; src: string } | null>(null);
  const key = `${runId}/${memberId}/${sessionId}`;
  useEffect(() => {
    if (!enabled) return;
    const ctl = new AbortController();
    const onFrame = (b64: string) => setFrame({ key, src: `data:image/jpeg;base64,${b64}` });
    void followFrames(runId, memberId, onFrame, ctl.signal).catch(() => undefined);
    return () => ctl.abort();
  }, [runId, memberId, key, enabled]);
  return enabled && frame?.key === key ? frame.src : null;
}

/**
 * Saved screenshots as data: URLs, by path (the CSP allows no blob: images, and an <img> cannot send
 * the bearer token). When the list grows only the new shots are fetched; a failed one is retried on
 * the next change. Entries for paths no longer listed (or another run's) are never returned.
 */
function useShots(runId: string, shots: BrowserTrail["shots"], enabled: boolean): Record<string, string> {
  const [loaded, setLoaded] = useState<{ runId: string; urls: Record<string, string> }>({ runId, urls: {} });
  const fetching = useRef<{ runId: string; ctl: AbortController; paths: Set<string> } | null>(null);
  const key = shots.map((s) => s.path).join("|");
  useEffect(() => {
    if (!enabled || !key) return;
    let cur = fetching.current;
    if (!cur || cur.runId !== runId) {
      cur?.ctl.abort();
      cur = fetching.current = { runId, ctl: new AbortController(), paths: new Set() };
    }
    const { ctl, paths } = cur;
    for (const path of key.split("|")) {
      if (paths.has(path)) continue; // loaded or on its way
      paths.add(path);
      void artifactImage(runId, path, ctl.signal)
        .then((src) => {
          if (ctl.signal.aborted) return;
          if (!src) return void paths.delete(path);
          setLoaded((l) => ({ runId, urls: { ...(l.runId === runId ? l.urls : {}), [path]: src } }));
        })
        .catch(() => paths.delete(path)); // the screenshot stays a placeholder
    }
  }, [runId, key, enabled]);
  useEffect(() => () => {
    fetching.current?.ctl.abort();
    fetching.current = null;
  }, []);
  const out: Record<string, string> = {};
  if (loaded.runId === runId) for (const s of shots) if (loaded.urls[s.path]) out[s.path] = loaded.urls[s.path]!;
  return out;
}

function Actions({ name, actions }: { name: string; actions: BrowserTrail["actions"] }) {
  return (
    <ol aria-label={`Actions by ${name}`} className="flex flex-col gap-1 text-sm">
      {actions.map((a) => (
        <li key={a.seq} className={a.ok ? "text-fg-muted" : "text-warning"}>
          <span className="font-mono">{a.action}</span>
          {a.target && <> <span className="font-mono break-all">{a.target}</span></>}
          {!a.ok && <>: {a.error ?? "failed"}</>}
        </li>
      ))}
    </ol>
  );
}

function MemberBrowser({ runId, name, memberId, trail, live }: { runId: string; name: string; memberId: string; trail: BrowserTrail; live: boolean }) {
  const streaming = live && trail.open;
  const frame = useFrames(runId, memberId, trail.sessionId, streaming);
  const urls = useShots(runId, trail.shots, !streaming);
  return (
    <section aria-label={`${name}'s browser`} className="rounded-lg border border-border bg-surface p-3 flex flex-col gap-3">
      <header className="flex items-center gap-2">
        <Globe className="size-4 text-fg-subtle shrink-0" aria-hidden />
        <h3 className="text-sm font-semibold">{name}</h3>
        <Badge tone={streaming ? "success" : "neutral"}>{streaming ? "live" : trail.open ? "open" : "closed"}</Badge>
      </header>
      <p className="font-mono text-xs text-fg-muted break-all" aria-label="Current page">
        {trail.url || "No page yet"}
        {trail.title && <span className="text-fg-subtle"> · {trail.title}</span>}
      </p>
      {streaming ? (
        <>
          <div data-frame-area className="rounded-md border border-border bg-surface-2 overflow-hidden aspect-video flex items-center justify-center">
            {frame ? (
              <img src={frame} alt={`Live view of ${name}'s browser`} className="w-full h-full object-contain select-none" draggable={false} />
            ) : (
              <span className="text-sm text-fg-subtle">Waiting for the first frame</span>
            )}
          </div>
          <Actions name={name} actions={trail.actions.slice(-LIVE_ACTIONS)} />
        </>
      ) : (
        <>
          {trail.shots.length === 0 ? (
            <p className="text-sm text-fg-subtle">No screenshots were kept for this session.</p>
          ) : (
            <div className="grid gap-3 sm:grid-cols-2">
              {trail.shots.map((s) => (
                <div key={s.path} className="flex flex-col gap-1">
                  <div className="rounded-md border border-border bg-surface-2 overflow-hidden aspect-video flex items-center justify-center">
                    {urls[s.path] ? (
                      <img src={urls[s.path]} alt={`Screenshot ${s.n} of ${s.url}`} className="w-full h-full object-contain" />
                    ) : (
                      <span className="text-xs text-fg-subtle">Loading screenshot {s.n}</span>
                    )}
                  </div>
                  <p className="font-mono text-xs text-fg-muted break-all">{s.n}. {s.url}</p>
                </div>
              ))}
            </div>
          )}
          <Actions name={name} actions={trail.actions} />
        </>
      )}
    </section>
  );
}

export function BrowserView({ runId, run, live }: { runId: string; run: RunState; live: boolean }) {
  const entries = Object.entries(run.browser ?? {});
  if (!entries.length) return <EmptyState title="No browser sessions" body="No member has opened a browser in this run." />;
  const nameOf = (id: string) => run.members.find((m) => m.id === id)?.name ?? id;
  return (
    <div className="p-3 grid gap-3 content-start">
      {entries.map(([id, trail]) => (
        <MemberBrowser key={id} runId={runId} memberId={id} name={nameOf(id)} trail={trail} live={live} />
      ))}
    </div>
  );
}
