import { FileText, Search, Trash2 } from "lucide-react";
import { useState, type FormEvent } from "react";

import { ConfirmDelete } from "@/components/connections/ConfirmDelete";
import { Button, Callout, EmptyState, ErrorState, IconButton, Skeleton } from "@/components/ui";
import { highlight } from "@/features/knowledge/highlight";
import { useDeleteDocument, useDocuments, useKnowledgeSearch } from "@/features/knowledge/queries";
import type { KnowledgeDocument } from "@/features/knowledge/types";
import { relTime } from "@/lib/format";
import { toast } from "@/stores/toast";

export function bytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

export function Highlighted({ text, terms }: { text: string; terms: string[] }) {
  return (
    <>
      {highlight(text, terms).map((s, i) =>
        s.hit ? <mark key={i} className="rounded-sm bg-warning-subtle text-fg px-0.5">{s.text}</mark> : <span key={i}>{s.text}</span>,
      )}
    </>
  );
}

export function DocumentsTable({ kbId }: { kbId: string }) {
  const docs = useDocuments(kbId);
  const del = useDeleteDocument(kbId);
  const [target, setTarget] = useState<KnowledgeDocument | null>(null);

  if (docs.isLoading) return <Skeleton className="h-24" />;
  if (docs.error) return <ErrorState error={docs.error} onRetry={() => void docs.refetch()} />;
  if (!docs.data?.length) return <EmptyState icon={FileText} title="No documents yet" body="Upload Markdown, text or HTML files above." />;

  return (
    <>
      <div className="overflow-x-auto rounded-md border border-border">
        <table className="w-full text-sm">
          <caption className="sr-only">Documents</caption>
          <thead className="bg-surface-2 text-left text-xs text-fg-muted">
            <tr>
              <th scope="col" className="px-3 py-2 font-medium">Name</th>
              <th scope="col" className="px-3 py-2 font-medium text-right">Chunks</th>
              <th scope="col" className="px-3 py-2 font-medium text-right hidden sm:table-cell">Size</th>
              <th scope="col" className="px-3 py-2 font-medium hidden md:table-cell">Added</th>
              <th scope="col" className="px-3 py-2"><span className="sr-only">Actions</span></th>
            </tr>
          </thead>
          <tbody>
            {docs.data.map((d) => (
              <tr key={d.id} className="border-t border-border">
                <td className="px-3 py-2 max-w-[10rem] sm:max-w-none">
                  <span className="block truncate font-medium" title={d.filename}>{d.filename}</span>
                  <span className="text-xs text-fg-subtle">{d.content_type}</span>
                </td>
                <td className="px-3 py-2 text-right tabular-nums">{d.chunk_count}</td>
                <td className="px-3 py-2 text-right tabular-nums hidden sm:table-cell">{bytes(d.size)}</td>
                <td className="px-3 py-2 text-fg-muted hidden md:table-cell">{relTime(d.created_at)}</td>
                <td className="px-3 py-2 text-right">
                  <IconButton label={`Delete ${d.filename}`} icon={Trash2} variant="danger-ghost" onClick={() => setTarget(d)} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <ConfirmDelete open={target !== null} onOpenChange={(o) => { if (!o) { setTarget(null); del.reset(); } }}
        kind="document" name={target?.filename ?? ""} pending={del.isPending} error={del.error}
        consequence="Its chunks are removed from search and from agents' knowledge."
        onConfirm={() => target && del.mutate(target.id, {
          onSuccess: () => { toast({ tone: "success", title: `Deleted ${target.filename}` }); setTarget(null); },
        })} />
    </>
  );
}

export function SearchPanel({ kbId }: { kbId: string }) {
  const [draft, setDraft] = useState("");
  const [query, setQuery] = useState("");
  const search = useKnowledgeSearch(kbId, query);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    setQuery(draft.trim());
  };
  const data = search.data;
  return (
    <div className="space-y-3">
      <form role="search" onSubmit={submit} className="flex gap-2">
        <label htmlFor={`kb-search-${kbId}`} className="sr-only">Search this knowledge base</label>
        <input id={`kb-search-${kbId}`} type="search" value={draft} onChange={(e) => setDraft(e.target.value)} maxLength={500}
          placeholder="Search passages…"
          className="h-9 min-w-0 flex-1 rounded-md border border-border-strong bg-surface px-3 text-base text-fg placeholder:text-fg-subtle" />
        <Button type="submit" icon={Search} loading={search.isFetching} disabled={!draft.trim()}>Search</Button>
      </form>
      {search.error && <Callout tone="danger" title="Search failed">{(search.error as Error).message}</Callout>}
      {query && data && (
        <div aria-live="polite">
          <p className="text-xs text-fg-muted mb-2">
            {data.results.length ? `${data.results.length} result${data.results.length === 1 ? "" : "s"} for “${data.query}”` : `No passages match “${data.query}”.`}
          </p>
          <ol className="space-y-2" aria-label="Search results">
            {data.results.map((r, i) => (
              <li key={`${r.document_id}-${r.chunk_index}`} className="rounded-md border border-border p-3">
                <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1 text-xs text-fg-muted">
                  <span className="font-medium text-fg break-all">{i + 1}. {r.filename} <span className="text-fg-subtle">· chunk {r.chunk_index}</span></span>
                  <span className="tabular-nums">score {r.score.toFixed(2)}</span>
                </div>
                <p className="mt-1.5 text-sm leading-relaxed break-words"><Highlighted text={r.snippet} terms={data.terms} /></p>
                <p className="mt-1 font-mono text-2xs text-fg-subtle break-all">{r.citation}</p>
              </li>
            ))}
          </ol>
        </div>
      )}
    </div>
  );
}
