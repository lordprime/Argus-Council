// Link knowledge to a run explicitly, before it ends: live agents see newly linked knowledge bases from their next turn.

import { BookOpen, BookPlus, Link2, X } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router";

import { KnowledgePicker, QuickAddKnowledgeDialog } from "@/components/knowledge/AddKnowledge";
import { Button, Callout, Dialog, IconButton, Skeleton } from "@/components/ui";
import { useKnowledgeBases, useLinkKnowledge, useRunKnowledge } from "@/features/knowledge/queries";
import { errorMessage } from "@/lib/api";
import { toast } from "@/stores/toast";

export function RunKnowledgeButton({ runId, topic, live }: { runId: string; topic: string; live: boolean }) {
  const [open, setOpen] = useState(false);
  const linked = useRunKnowledge(runId);
  const n = linked.data?.length ?? 0;
  return (
    <>
      <Button icon={BookOpen} aria-haspopup="dialog" onClick={() => setOpen(true)}>
        Knowledge{n > 0 && <span className="tabular text-fg-subtle"> · {n}</span>}
      </Button>
      {open && <RunKnowledgeDialog runId={runId} topic={topic} live={live} onOpenChange={setOpen} />}
    </>
  );
}

function RunKnowledgeDialog({ runId, topic, live, onOpenChange }: { runId: string; topic: string; live: boolean; onOpenChange: (o: boolean) => void }) {
  const bases = useKnowledgeBases();
  const linked = useRunKnowledge(runId);
  const { link, unlink } = useLinkKnowledge(runId);
  const [picked, setPicked] = useState<string[]>([]);
  const [adding, setAdding] = useState(false);
  const linkedIds = linked.data?.map((k) => k.id) ?? [];

  const linkIds = (ids: string[]) =>
    link.mutate(ids, {
      onSuccess: (r) => {
        setPicked([]);
        if (r.added.length) {
          toast({ tone: "success", title: `Linked ${r.added.length} knowledge base${r.added.length === 1 ? "" : "s"}`,
            body: r.live ? "The agents use it from their next turn." : undefined });
        }
      },
    });

  return (
    <Dialog open onOpenChange={onOpenChange} wide title="Run knowledge"
      description={live
        ? "Link knowledge bases while the council works. Agents are told about them and search them before the web."
        : "Knowledge bases linked to this run. Linking more now only affects follow-up runs that reuse this setup."}>
      <div className="flex flex-col gap-5">
        <section aria-labelledby="run-kb-linked" className="flex flex-col gap-2">
          <h3 id="run-kb-linked" className="text-sm font-semibold">Linked</h3>
          {linked.isLoading ? <Skeleton className="h-10" /> : linked.data?.length ? (
            <ul className="divide-y divide-border rounded-md border border-border">
              {linked.data.map((k) => (
                <li key={k.id} className="flex items-center gap-3 px-3 py-2 text-sm">
                  <Link2 className="size-4 text-accent shrink-0" aria-hidden />
                  <span className="min-w-0 flex-1">
                    <Link to={`/knowledge?kb=${k.id}`} className="block truncate font-medium hover:underline">{k.name}</Link>
                    <span className="block text-xs text-fg-subtle">{k.document_count} document{k.document_count === 1 ? "" : "s"}</span>
                  </span>
                  <IconButton label={`Unlink ${k.name}`} icon={X} variant="ghost" loading={unlink.isPending && unlink.variables === k.id}
                    onClick={() => unlink.mutate(k.id)} />
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-sm text-fg-muted">Nothing linked yet. Agents can still search all your knowledge and the research library.</p>
          )}
        </section>

        <section aria-labelledby="run-kb-more" className="flex flex-col gap-3">
          <h3 id="run-kb-more" className="sr-only">Link more</h3>
          <KnowledgePicker legend="Link more" bases={bases.data ?? []} value={picked} onChange={setPicked} disabledIds={linkedIds} />
          {(link.error ?? unlink.error) != null && <Callout tone="danger" title="Could not update">{errorMessage(link.error ?? unlink.error)}</Callout>}
          <div className="flex flex-wrap justify-between gap-2">
            <Button variant="ghost" size="sm" icon={BookPlus} onClick={() => setAdding(true)}>Add a note or link…</Button>
            <Button variant="primary" size="sm" icon={Link2} disabled={!picked.length} loading={link.isPending} onClick={() => linkIds(picked)}>
              Link {picked.length || ""} to this run
            </Button>
          </div>
        </section>
      </div>
      {adding && (
        <QuickAddKnowledgeDialog open onOpenChange={setAdding} bases={bases.data ?? []} defaultName={`Notes: ${topic}`.slice(0, 120)}
          onAdded={(id) => linkIds([id])} />
      )}
    </Dialog>
  );
}
