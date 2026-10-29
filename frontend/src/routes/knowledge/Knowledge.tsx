import { BookOpen, Globe, Library, Plus, Trash2 } from "lucide-react";
import { useState, type FormEvent } from "react";
import { useSearchParams } from "react-router";

import { ConfirmDelete } from "@/components/connections/ConfirmDelete";
import { AddKnowledgeForm } from "@/components/knowledge/AddKnowledge";
import { MemoryPanel } from "@/components/knowledge/MemoryPanel";
import { DocumentsTable, SearchPanel } from "@/components/knowledge/parts";
import { UploadZone } from "@/components/knowledge/UploadZone";
import { Badge, Button, Callout, Card, Dialog, EmptyState, ErrorState, Page, PageHeader, Skeleton, Tabs, TextArea, TextField } from "@/components/ui";
import { useCreateKnowledgeBase, useDeleteKnowledgeBase, useKnowledgeBases } from "@/features/knowledge/queries";
import type { KnowledgeBase } from "@/features/knowledge/types";
import { errorMessage } from "@/lib/api";
import { cn } from "@/lib/cn";
import { toast } from "@/stores/toast";

function CreateDialog({ open, onOpenChange, onCreated }: { open: boolean; onOpenChange: (o: boolean) => void; onCreated: (kb: KnowledgeBase) => void }) {
  const create = useCreateKnowledgeBase();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const submit = (e: FormEvent) => {
    e.preventDefault();
    create.mutate({ name: name.trim(), description }, {
      onSuccess: (kb) => {
        toast({ tone: "success", title: `Created ${kb.name}` });
        setName("");
        setDescription("");
        onCreated(kb);
        onOpenChange(false);
      },
    });
  };
  return (
    <Dialog open={open} onOpenChange={(o) => { onOpenChange(o); if (!o) create.reset(); }} title="New knowledge base"
      description="A named collection of documents your agents can search with knowledge_search.">
      <form onSubmit={submit} className="space-y-3">
        <TextField label="Name" value={name} onChange={(e) => setName(e.target.value)} required maxLength={120} autoFocus />
        <TextArea label="Description" value={description} onChange={(e) => setDescription(e.target.value)} maxLength={2000} />
        {create.error != null && <Callout tone="danger" title="Could not create">{errorMessage(create.error)}</Callout>}
        <div className="flex justify-end gap-2">
          <Button type="button" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button type="submit" variant="primary" loading={create.isPending} disabled={!name.trim()}>Create</Button>
        </div>
      </form>
    </Dialog>
  );
}

function KnowledgeBasePane({ kb, onDeleted }: { kb: KnowledgeBase; onDeleted: () => void }) {
  const del = useDeleteKnowledgeBase();
  const [confirm, setConfirm] = useState(false);
  const pct = Math.min(100, Math.round((kb.chunk_count / kb.max_chunks) * 100));
  const library = kb.kind === "library";
  return (
    <Card className="p-4 space-y-5 min-w-0" aria-labelledby={`kb-title-${kb.id}`}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 id={`kb-title-${kb.id}`} className="text-lg break-words flex flex-wrap items-center gap-2">
            {kb.name}{library && <Badge tone="info"><Globe className="size-3" aria-hidden />Auto-captured</Badge>}
          </h2>
          {kb.description && <p className="text-sm text-fg-muted mt-0.5 break-words">{kb.description}</p>}
          <p className="text-xs text-fg-subtle mt-1">
            {kb.document_count} document{kb.document_count === 1 ? "" : "s"} · {kb.chunk_count} / {kb.max_chunks} chunks ({pct}%)
          </p>
          {library && (
            <p className="text-xs text-fg-subtle mt-1">Full: the least recently used sources make room for new ones. Every run's knowledge_search covers it.</p>
          )}
        </div>
        <Button variant="danger-ghost" size="sm" icon={Trash2} onClick={() => setConfirm(true)}>Delete</Button>
      </div>
      <div className="grid gap-5 xl:grid-cols-2">
        <section aria-labelledby={`kb-upload-${kb.id}`} className="space-y-2 min-w-0">
          <h3 id={`kb-upload-${kb.id}`} className="text-sm font-semibold">Upload files</h3>
          <UploadZone kbId={kb.id} />
        </section>
        <section aria-labelledby={`kb-add-${kb.id}`} className="space-y-2 min-w-0">
          <h3 id={`kb-add-${kb.id}`} className="text-sm font-semibold">Add text or a link</h3>
          <AddKnowledgeForm kbId={kb.id} />
        </section>
      </div>
      <section aria-labelledby={`kb-docs-${kb.id}`} className="space-y-2">
        <h3 id={`kb-docs-${kb.id}`} className="text-sm font-semibold">Documents</h3>
        <DocumentsTable kbId={kb.id} />
      </section>
      <section aria-labelledby={`kb-search-h-${kb.id}`} className="space-y-2">
        <h3 id={`kb-search-h-${kb.id}`} className="text-sm font-semibold">Search</h3>
        <SearchPanel key={kb.id} kbId={kb.id} />
      </section>
      <ConfirmDelete open={confirm} onOpenChange={(o) => { setConfirm(o); if (!o) del.reset(); }} kind="knowledge base" name={kb.name}
        consequence={`All ${kb.document_count} documents and their chunks are deleted. Agents can no longer search it.`}
        pending={del.isPending} error={del.error}
        onConfirm={() => del.mutate(kb.id, {
          onSuccess: () => { toast({ tone: "success", title: `Deleted ${kb.name}` }); setConfirm(false); onDeleted(); },
        })} />
    </Card>
  );
}

function BasesTab() {
  const bases = useKnowledgeBases();
  const [params, setParams] = useSearchParams();
  const [creating, setCreating] = useState(false);
  const select = (id: string | null) => {
    const next = new URLSearchParams(params);
    if (id) next.set("kb", id);
    else next.delete("kb");
    setParams(next, { replace: true });
  };
  const list = bases.data ?? [];
  const selected = list.find((k) => k.id === params.get("kb")) ?? list[0] ?? null;

  if (bases.isLoading) return <div className="pt-4 space-y-2"><Skeleton className="h-10" /><Skeleton className="h-40" /></div>;
  if (bases.error) return <ErrorState error={bases.error} onRetry={() => void bases.refetch()} />;

  return (
    <div className="pt-4">
      <CreateDialog open={creating} onOpenChange={setCreating} onCreated={(kb) => select(kb.id)} />
      {!list.length ? (
        <EmptyState icon={Library} title="No knowledge bases yet"
          body="Create one, upload documents, and agents in your councils can search them with citations."
          action={<Button variant="primary" icon={Plus} onClick={() => setCreating(true)}>New knowledge base</Button>} />
      ) : (
        <div className="grid gap-4 lg:grid-cols-[16rem_minmax(0,1fr)]">
          <nav aria-label="Knowledge bases" className="space-y-2 min-w-0">
            <Button variant="primary" icon={Plus} className="w-full" onClick={() => setCreating(true)}>New knowledge base</Button>
            <ul className="space-y-1">
              {list.map((kb) => (
                <li key={kb.id}>
                  <button type="button" onClick={() => select(kb.id)} aria-current={selected?.id === kb.id ? "true" : undefined}
                    className={cn("w-full rounded-md px-3 py-2 text-left text-sm transition-colors",
                      selected?.id === kb.id ? "bg-accent-subtle text-fg font-medium" : "hover:bg-surface-2 text-fg-muted")}>
                    <span className="flex items-center gap-1.5 truncate">
                      {kb.kind === "library" && <Globe className="size-3.5 shrink-0 text-accent" aria-label="Research library" />}
                      <span className="truncate">{kb.name}</span>
                    </span>
                    <span className="block text-xs text-fg-subtle">{kb.document_count} docs · {kb.chunk_count} chunks</span>
                  </button>
                </li>
              ))}
            </ul>
          </nav>
          {selected && <KnowledgeBasePane key={selected.id} kb={selected} onDeleted={() => select(null)} />}
        </div>
      )}
    </div>
  );
}

export default function KnowledgePage() {
  const [params, setParams] = useSearchParams();
  const tab = params.get("tab") === "memory" ? "memory" : "bases";
  return (
    <Page>
      <PageHeader title="Knowledge"
        description="What your agents know: documents and notes you add, the research library of every source they have read, and the memory they keep across runs." />
      <Tabs value={tab}
        onValueChange={(v) => {
          const next = new URLSearchParams(params);
          if (v === "memory") next.set("tab", "memory");
          else next.delete("tab");
          setParams(next, { replace: true });
        }}
        tabs={[
          { value: "bases", label: <span className="inline-flex items-center gap-1.5"><BookOpen className="size-4" aria-hidden />Knowledge bases</span>, content: <BasesTab /> },
          { value: "memory", label: "Memory", content: <MemoryPanel /> },
        ]} />
    </Page>
  );
}
