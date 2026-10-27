import { History, RotateCcw } from "lucide-react";
import { useMemo, useState } from "react";

import { Badge, Button, Callout, Card, Dialog, ErrorState, Skeleton } from "@/components/ui";
import { diffStats, lineDiff, snapshotText } from "@/features/skills/diff";
import { useRestoreVersion, useSkillVersion, useSkillVersions, type RestoreResult } from "@/features/skills/queries";
import { errorMessage } from "@/lib/api";
import { cn } from "@/lib/cn";
import { relTime } from "@/lib/format";
import { toast } from "@/stores/toast";

const MARK = { same: " ", add: "+", del: "-" } as const;
const ROW = {
  same: "text-fg-muted",
  add: "bg-success-subtle text-fg",
  del: "bg-danger-subtle text-fg",
} as const;

function DiffView({ name, n, current }: { name: string; n: number; current: { skill_md: string; files: Record<string, string> } }) {
  const v = useSkillVersion(name, n);
  const ops = useMemo(
    () => (v.data ? lineDiff(snapshotText(v.data.skill_md, v.data.file_contents), snapshotText(current.skill_md, current.files)) : []),
    [v.data, current.skill_md, current.files],
  );
  if (v.isLoading) return <div role="status" aria-busy="true"><span className="sr-only">Loading version {n}…</span><Skeleton className="h-40" /></div>;
  if (v.error) return <ErrorState error={v.error} onRetry={() => void v.refetch()} />;
  const { added, removed } = diffStats(ops);
  return (
    <div className="flex flex-col gap-2 min-w-0">
      <p className="text-sm">
        Changes from <strong>v{n}</strong> to the current skill:{" "}
        <span className="text-success font-medium">+{added}</span>{" "}
        <span className="text-danger font-medium">−{removed}</span>{" "}
        <span className="sr-only">lines added and removed</span>
      </p>
      {added + removed === 0 ? (
        <p className="text-sm text-fg-subtle">Identical to the current skill.</p>
      ) : (
        <pre tabIndex={0} role="region" aria-label={`Diff of version ${n} against current`}
          className="max-h-96 overflow-auto rounded-md border border-border bg-surface-2 py-2 font-mono text-xs leading-5 whitespace-pre-wrap break-words">
          {ops.map((o, i) => (
            <div key={i} className={cn("px-3", ROW[o.kind])}>
              <span aria-hidden className="select-none inline-block w-3">{MARK[o.kind]}</span>
              {o.kind !== "same" && <span className="sr-only">{o.kind === "add" ? "added: " : "removed: "}</span>}
              {o.text || " "}
            </div>
          ))}
        </pre>
      )}
    </div>
  );
}

/** Immutable version history for a user skill: pick a version to diff against the current one and restore it. */
export function VersionHistory({ name, current, onRestored }: {
  name: string;
  current: { skill_md: string; files: Record<string, string> };
  onRestored?: (r: RestoreResult) => void;
}) {
  const q = useSkillVersions(name);
  const restore = useRestoreVersion();
  const [selected, setSelected] = useState<number | null>(null);
  const [confirm, setConfirm] = useState<number | null>(null);
  const versions = q.data?.versions ?? [];
  const latest = q.data?.current ?? null;
  const shown = selected ?? versions.find((v) => v.number !== latest)?.number ?? null;

  const doRestore = (n: number) =>
    restore.mutate({ name, n }, {
      onSuccess: (r) => {
        setConfirm(null);
        setSelected(null);
        void q.refetch();
        toast({ tone: "success", title: `Restored ${name} to v${n}`, body: r.current_version ? `Saved as version ${r.current_version}.` : undefined });
        onRestored?.(r);
      },
    });

  return (
    <Card className="p-5 flex flex-col gap-4" aria-labelledby="history-h">
      <div>
        <h2 id="history-h" className="text-lg inline-flex items-center gap-2"><History className="size-5" aria-hidden />Version history</h2>
        <p className="text-sm text-fg-muted">Every save keeps an immutable snapshot of SKILL.md and its files. Restoring creates a new version.</p>
      </div>
      {q.isLoading ? (
        <div role="status" aria-busy="true"><span className="sr-only">Loading versions…</span><Skeleton className="h-24" /></div>
      ) : q.error ? (
        <ErrorState error={q.error} onRetry={() => void q.refetch()} />
      ) : !versions.length ? (
        <p className="text-sm text-fg-subtle">No versions recorded yet.</p>
      ) : (
        <div className="grid gap-4 lg:grid-cols-[16rem_minmax(0,1fr)]">
          <ol aria-label={`Versions of ${name}`} className="flex flex-col gap-2 min-w-0">
            {versions.map((v) => {
              const isCurrent = v.number === latest;
              const on = shown === v.number;
              return (
                <li key={v.number} className={cn("rounded-md border p-2.5 flex flex-col gap-1.5 min-w-0", on ? "border-accent bg-accent-subtle/40" : "border-border")}>
                  <div className="flex flex-wrap items-center gap-1.5">
                    <span className="font-mono text-sm font-semibold">v{v.number}</span>
                    {v.version && <span className="font-mono text-xs text-fg-subtle">{v.version}</span>}
                    {isCurrent && <Badge tone="success">Current</Badge>}
                  </div>
                  <p className="text-xs text-fg-muted break-words">
                    {v.note || "saved"} · <time dateTime={v.created_at} title={new Date(v.created_at).toLocaleString()}>{relTime(v.created_at)}</time>
                    {v.author && <> · <span className="break-all">{v.author}</span></>}
                  </p>
                  {!isCurrent && (
                    <div className="flex flex-wrap gap-1.5">
                      <Button size="sm" variant="ghost" aria-pressed={on} onClick={() => setSelected(v.number)}>
                        {on ? "Comparing" : "Compare"}<span className="sr-only"> version {v.number}</span>
                      </Button>
                      <Button size="sm" icon={RotateCcw} onClick={() => { restore.reset(); setConfirm(v.number); }}>
                        Restore<span className="sr-only"> version {v.number}</span>
                      </Button>
                    </div>
                  )}
                </li>
              );
            })}
          </ol>
          <div aria-live="polite" className="min-w-0">
            {shown !== null ? (
              <DiffView name={name} n={shown} current={current} />
            ) : (
              <p className="text-sm text-fg-subtle">Only one version so far. Save a change to compare versions.</p>
            )}
          </div>
        </div>
      )}

      <Dialog open={confirm !== null} onOpenChange={(o) => !o && setConfirm(null)} title={`Restore ${name} to v${confirm ?? ""}?`}
        description="The current content stays in the history; restoring saves a new version. Unsaved edits in the editor are discarded."
        footer={
          <>
            <Button onClick={() => setConfirm(null)}>Cancel</Button>
            <Button variant="primary" icon={RotateCcw} loading={restore.isPending} onClick={() => confirm !== null && doRestore(confirm)}>
              Restore v{confirm}
            </Button>
          </>
        }>
        {restore.error ? <Callout tone="danger" title="Could not restore">{errorMessage(restore.error)}</Callout> : null}
      </Dialog>
    </Card>
  );
}
