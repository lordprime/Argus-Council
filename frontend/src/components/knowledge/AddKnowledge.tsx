// Add knowledge without a file: pasted text (notes, an abstract, an article) or a page fetched from a URL,
// and a checkbox picker of knowledge bases (run composer, run workspace).

import { Library, Link2, Plus } from "lucide-react";
import { useState, type FormEvent } from "react";

import { Badge, Button, Callout, Dialog, Segmented, SelectField, TextArea, TextField } from "@/components/ui";
import { useAddKnowledge, useCreateKnowledgeBase, type NewKnowledge } from "@/features/knowledge/queries";
import type { KnowledgeBase, KnowledgeDocument } from "@/features/knowledge/types";
import { errorMessage } from "@/lib/api";
import { toast } from "@/stores/toast";

type Kind = NewKnowledge["kind"];

/** ``kbId`` may be a function that resolves (e.g. creates) the knowledge base when the form is submitted. */
export function AddKnowledgeForm({ kbId, onAdded, compact, disabled }: {
  kbId: string | (() => Promise<string>);
  onAdded?: (doc: KnowledgeDocument) => void;
  compact?: boolean;
  disabled?: boolean;
}) {
  const add = useAddKnowledge();
  const [kind, setKind] = useState<Kind>("text");
  const [title, setTitle] = useState("");
  const [text, setText] = useState("");
  const [url, setUrl] = useState("");
  const ready = kind === "text" ? Boolean(title.trim() && text.trim()) : /^https?:\/\/\S+$/i.test(url.trim());

  const [resolveError, setResolveError] = useState<unknown>(null);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!ready || disabled) return;
    const item: NewKnowledge = kind === "text"
      ? { kind, title: title.trim(), text }
      : { kind, url: url.trim(), title: title.trim() || null };
    let id: string;
    try {
      setResolveError(null);
      id = typeof kbId === "string" ? kbId : await kbId();
    } catch (err) {
      setResolveError(err);
      return;
    }
    add.mutate({ kbId: id, item }, {
      onSuccess: (doc) => {
        toast({ tone: "success", title: `Added ${doc.title || doc.filename}`, body: `${doc.chunk_count} searchable chunk${doc.chunk_count === 1 ? "" : "s"}` });
        setTitle("");
        setText("");
        setUrl("");
        onAdded?.(doc);
      },
    });
  };

  return (
    <form onSubmit={(e) => void submit(e)} className="flex flex-col gap-3" aria-label="Add knowledge">
      <Segmented label="Add" hideLabel size="sm" value={kind} onChange={(v) => { setKind(v); add.reset(); }}
        options={[{ value: "text", label: "Paste text", icon: Plus }, { value: "url", label: "From a URL", icon: Link2 }]} />
      {kind === "url" && (
        <TextField label="URL" type="url" inputMode="url" value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://arxiv.org/abs/…"
          spellCheck={false} hint="The page is fetched now (same safety checks as the agents' web_fetch) and its text indexed." />
      )}
      <TextField label={kind === "url" ? "Title (optional)" : "Title"} value={title} onChange={(e) => setTitle(e.target.value)} maxLength={300}
        required={kind === "text"} placeholder={kind === "url" ? "Defaults to the page title" : "e.g. Meeting notes, paper abstract"} />
      {kind === "text" && (
        <TextArea label="Text" value={text} onChange={(e) => setText(e.target.value)} rows={compact ? 4 : 6} required
          hint="Notes, an abstract, an article, anything agents should know." />
      )}
      {(add.error ?? resolveError) != null && <Callout tone="danger" title="Could not add it">{errorMessage(add.error ?? resolveError)}</Callout>}
      <div className="flex justify-end">
        <Button type="submit" size="sm" variant="primary" icon={Plus} loading={add.isPending} disabled={!ready || disabled}>Add to knowledge base</Button>
      </div>
    </form>
  );
}

/** Checkbox list of knowledge bases. The research library is always searched, so it is shown but fixed. */
export function KnowledgePicker({ bases, value, onChange, legend = "Knowledge", disabledIds = [] }: {
  bases: KnowledgeBase[];
  value: string[];
  onChange: (ids: string[]) => void;
  legend?: string;
  disabledIds?: string[];
}) {
  const toggle = (id: string, on: boolean) => onChange(on ? [...value, id] : value.filter((v) => v !== id));
  const library = bases.find((b) => b.kind === "library");
  const own = bases.filter((b) => b.kind !== "library");
  return (
    <fieldset className="flex flex-col gap-2 min-w-0">
      <legend className="text-sm font-medium mb-1">{legend}</legend>
      {library && (
        <p className="flex items-center gap-2 text-xs text-fg-subtle">
          <Library className="size-3.5 shrink-0" aria-hidden />
          <span>{library.name} ({library.document_count} sources) is always searched.</span>
        </p>
      )}
      {own.length ? (
        <ul className="grid gap-1.5 sm:grid-cols-2">
          {own.map((kb) => {
            const fixed = disabledIds.includes(kb.id);
            return (
              <li key={kb.id}>
                <label className="flex items-start gap-2 rounded-md border border-border px-3 py-2 text-sm hover:bg-surface-2 has-[:checked]:border-accent has-[:checked]:bg-accent-subtle">
                  <input type="checkbox" className="size-4 mt-0.5 accent-[rgb(var(--accent))]" checked={fixed || value.includes(kb.id)} disabled={fixed}
                    onChange={(e) => toggle(kb.id, e.target.checked)} />
                  <span className="min-w-0">
                    <span className="block truncate font-medium">{kb.name}</span>
                    <span className="block text-xs text-fg-subtle">{kb.document_count} document{kb.document_count === 1 ? "" : "s"}</span>
                  </span>
                  {fixed && <Badge className="ml-auto shrink-0">Linked</Badge>}
                </label>
              </li>
            );
          })}
        </ul>
      ) : (
        <p className="text-xs text-fg-subtle">No knowledge bases of your own yet. Create one on the Knowledge page.</p>
      )}
    </fieldset>
  );
}

const NEW = "__new__";

/**
 * Add a note or a link to a knowledge base (an existing one, or a new one made on submit) and hand its id back,
 * so the caller can link it to a run. Used by the run composer and the run workspace.
 */
export function QuickAddKnowledgeDialog({ open, onOpenChange, bases, defaultName, onAdded }: {
  open: boolean;
  onOpenChange: (o: boolean) => void;
  bases: KnowledgeBase[];
  defaultName: string;
  onAdded: (kbId: string) => void;
}) {
  const own = bases.filter((b) => b.kind !== "library");
  const create = useCreateKnowledgeBase();
  const [target, setTarget] = useState(own[0]?.id ?? NEW);
  const [name, setName] = useState(defaultName);
  const nameTaken = target === NEW && bases.some((b) => b.name === name.trim());
  const resolve = async () => {
    if (target !== NEW) return target;
    const kb = await create.mutateAsync({ name: name.trim(), description: "" });
    setTarget(kb.id);
    return kb.id;
  };
  return (
    <Dialog open={open} onOpenChange={onOpenChange} title="Add knowledge"
      description="Paste text or add a link. It is indexed right away, and the knowledge base is linked to this run.">
      <div className="flex flex-col gap-4">
        <div className="grid gap-3 sm:grid-cols-2">
          <SelectField label="Knowledge base" value={target} onChange={(e) => setTarget(e.target.value)}>
            {own.map((b) => <option key={b.id} value={b.id}>{b.name}</option>)}
            <option value={NEW}>New knowledge base…</option>
          </SelectField>
          {target === NEW && (
            <TextField label="Name" value={name} onChange={(e) => setName(e.target.value)} maxLength={120} required
              error={nameTaken ? "You already have a knowledge base with this name" : undefined} />
          )}
        </div>
        <AddKnowledgeForm kbId={resolve} compact disabled={target === NEW && (!name.trim() || nameTaken)}
          onAdded={(doc) => { onAdded(doc.kb_id); onOpenChange(false); }} />
      </div>
    </Dialog>
  );
}
