// The Browser view of a run: one card per member that opened a browser. While the run is live and the
// session open it shows the member's browser as it is now (watch-only: nothing here sends input to the
// page); otherwise it replays the screenshots the run kept, in order, with the actions between them.

import { Globe } from "lucide-react";
import { useEffect, useState } from "react";

import { Badge, EmptyState } from "@/components/ui";
import { auth, V1 } from "@/lib/api";
import { openFrames } from "@/lib/frames";
import type { BrowserTrail, RunState } from "@/stores/run";

const LIVE_ACTIONS = 5;

/** Stream the member's frames while mounted; the stream is aborted on unmount. */
function useFrames(runId: string, memberId: string, enabled: boolean): string | null {
  const [frame, setFrame] = useState<string | null>(null);
  useEffect(() => {
    if (!enabled) return;
    const ctl = new AbortController();
    void openFrames(runId, memberId, (b64) => setFrame(`data:image/jpeg;base64,${b64}`), ctl.signal).catch(() => undefined);
    return () => ctl.abort();
  }, [runId, memberId, enabled]);
  return enabled ? frame : null;
}

/** Saved screenshots as object URLs (an <img src> cannot send the bearer token); revoked on unmount. */
function useShots(runId: string, shots: BrowserTrail["shots"], enabled: boolean): Record<string, string> {
  const [urls, setUrls] = useState<Record<string, string>>({});
  const key = shots.map((s) => s.path).join("|");
  useEffect(() => {
    if (!enabled) return;
    let stopped = false;
    const made: string[] = [];
    for (const shot of shots) {
      void (async () => {
        try {
          const resp = await fetch(`${V1}/runs/${runId}/artifacts/download?path=${encodeURIComponent(shot.path)}`, {
            headers: { Authorization: `Bearer ${auth.token ?? ""}` },
          });
          if (!resp.ok) return;
          const url = URL.createObjectURL(await resp.blob());
          if (stopped) return void URL.revokeObjectURL(url);
          made.push(url);
          setUrls((u) => ({ ...u, [shot.path]: url }));
        } catch {
          /* the screenshot stays a placeholder */
        }
      })();
    }
    return () => {
      stopped = true;
      made.forEach((u) => URL.revokeObjectURL(u));
    };
    // `key` stands for the list of paths.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId, key, enabled]);
  return urls;
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
  const frame = useFrames(runId, memberId, streaming);
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
