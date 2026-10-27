import { Brain, Trash2 } from "lucide-react";
import { useMemo, useState } from "react";

import { ConfirmDelete } from "@/components/connections/ConfirmDelete";
import { Badge, EmptyState, ErrorState, IconButton, SelectField, Skeleton } from "@/components/ui";
import { useDeleteMemory, useMemory } from "@/features/knowledge/queries";
import type { MemoryItem } from "@/features/knowledge/types";
import { relTime } from "@/lib/format";
import { useCouncils } from "@/lib/queries";
import { toast } from "@/stores/toast";

export function MemoryPanel() {
  const [council, setCouncil] = useState("all");
  const councils = useCouncils();
  const memory = useMemory(council);
  const del = useDeleteMemory();
  const [target, setTarget] = useState<MemoryItem | null>(null);
  const names = useMemo(() => new Map((councils.data ?? []).map((c) => [c.id, c.name])), [councils.data]);

  return (
    <div className="space-y-4 pt-4">
      <p className="text-sm text-fg-muted">
        Long-term notes agents save with <code className="font-mono text-xs">memory_write</code> and recall in later runs of the same council.
      </p>
      <SelectField label="Council" value={council} onChange={(e) => setCouncil(e.target.value)} className="max-w-xs">
        <option value="all">All memory</option>
        <option value="global">Ad-hoc runs (no council)</option>
        {(councils.data ?? []).map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
      </SelectField>
      {memory.isLoading ? <Skeleton className="h-24" />
        : memory.error ? <ErrorState error={memory.error} onRetry={() => void memory.refetch()} />
        : !memory.data?.length ? <EmptyState icon={Brain} title="No memories yet" body="Agents with the memory tools will save notes here during runs." />
        : (
          <ul aria-label="Memory items" className="space-y-2">
            {memory.data.map((m) => (
              <li key={m.id} className="rounded-md border border-border bg-surface p-3">
                <div className="flex items-start gap-2">
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-1.5 text-xs text-fg-muted">
                      {m.key && <span className="font-mono text-fg">{m.key}</span>}
                      <span>{m.council_id ? names.get(m.council_id) ?? "Council" : "Ad-hoc"}</span>
                      {m.created_by && <span>· {m.created_by}</span>}
                      <span>· {relTime(m.updated_at)}</span>
                      {m.tags.map((t) => <Badge key={t}>{t}</Badge>)}
                    </div>
                    <p className="mt-1 text-sm whitespace-pre-wrap break-words">{m.text}</p>
                  </div>
                  <IconButton label={`Delete memory ${m.key || m.text.slice(0, 30)}`} icon={Trash2} variant="danger-ghost" onClick={() => setTarget(m)} />
                </div>
              </li>
            ))}
          </ul>
        )}
      <ConfirmDelete open={target !== null} onOpenChange={(o) => { if (!o) { setTarget(null); del.reset(); } }}
        kind="memory" name={target ? target.key || `${target.text.slice(0, 40)}${target.text.length > 40 ? "…" : ""}` : ""}
        pending={del.isPending} error={del.error}
        onConfirm={() => target && del.mutate(target.id, {
          onSuccess: () => { toast({ tone: "success", title: "Memory deleted" }); setTarget(null); },
        })} />
    </div>
  );
}
