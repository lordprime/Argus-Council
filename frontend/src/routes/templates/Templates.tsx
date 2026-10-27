import { LayoutTemplate, Pencil, Play, Plus, Trash2 } from "lucide-react";
import { useState, type FormEvent } from "react";

import { ModeLabel } from "@/components/run/ModeLabel";
import { Badge, Button, ButtonLink, Callout, Card, CardTitle, Dialog, EmptyState, ErrorState, IconButton, Page, PageHeader, Skeleton, TextArea, TextField } from "@/components/ui";
import { RunTemplateDialog } from "@/features/templates/components";
import { templateVariables, useDeleteTemplate, useTemplates, useUpdateTemplate, type RunTemplate } from "@/features/templates/queries";
import { errorMessage } from "@/lib/api";
import { relTime } from "@/lib/format";
import { toast } from "@/stores/toast";

function EditTemplateDialog({ template, onOpenChange }: { template: RunTemplate | null; onOpenChange: (o: boolean) => void }) {
  return (
    <Dialog open={Boolean(template)} onOpenChange={onOpenChange} title="Edit template" wide>
      {template && <EditForm key={template.id} template={template} onDone={() => onOpenChange(false)} />}
    </Dialog>
  );
}

function EditForm({ template, onDone }: { template: RunTemplate; onDone: () => void }) {
  const update = useUpdateTemplate();
  const [name, setName] = useState(template.name);
  const [summary, setSummary] = useState(template.summary);
  const [topic, setTopic] = useState(template.topic);
  const [description, setDescription] = useState(template.description);
  const vars = templateVariables(topic, description);
  const save = async (e: FormEvent) => {
    e.preventDefault();
    try {
      await update.mutateAsync({ id: template.id, patch: { name: name.trim(), summary, topic, description } });
      toast({ tone: "success", title: "Template saved" });
      onDone();
    } catch {
      /* shown below */
    }
  };
  return (
    <form onSubmit={save} className="flex flex-col gap-4">
      <TextField label="Template name" required maxLength={120} value={name} onChange={(e) => setName(e.target.value)} />
      <TextArea label="Notes" maxLength={500} value={summary} onChange={(e) => setSummary(e.target.value)} />
      <TextField label="Topic" required maxLength={500} value={topic} onChange={(e) => setTopic(e.target.value)}
        hint="Use {{variable}} placeholders, e.g. Market for {{product}} in {{region}}." />
      <TextArea label="Description" maxLength={20000} value={description} onChange={(e) => setDescription(e.target.value)} />
      <p className="text-xs text-fg-subtle">Variables: {vars.length ? vars.map((v) => `{{${v}}}`).join(", ") : "none"}</p>
      {update.error && <Callout tone="danger" title="Could not save">{errorMessage(update.error)}</Callout>}
      <div className="flex justify-end gap-2">
        <Button type="button" onClick={onDone}>Cancel</Button>
        <Button type="submit" variant="primary" loading={update.isPending} disabled={!name.trim() || !topic.trim()}>Save</Button>
      </div>
    </form>
  );
}

function TemplateCard({ t, onRun, onEdit }: { t: RunTemplate; onRun: () => void; onEdit: () => void }) {
  const del = useDeleteTemplate();
  const remove = () => {
    if (!window.confirm(`Delete the template “${t.name}”? Runs started from it are kept.`)) return;
    del.mutate(t.id, { onError: (e) => toast({ tone: "danger", title: "Could not delete", body: errorMessage(e) }) });
  };
  const headingId = `tpl-${t.id}`;
  return (
    <li>
      <Card as="article" aria-labelledby={headingId} className="p-4 flex flex-col gap-3 h-full">
        <div className="flex items-start gap-2">
          <div className="min-w-0 flex-1">
            <CardTitle as="h2" id={headingId} className="truncate">{t.name}</CardTitle>
            {t.summary && <p className="text-sm text-fg-muted mt-0.5">{t.summary}</p>}
          </div>
          <IconButton label={`Edit ${t.name}`} icon={Pencil} onClick={onEdit} />
          <IconButton label={`Delete ${t.name}`} icon={Trash2} variant="danger-ghost" loading={del.isPending} onClick={remove} />
        </div>
        <p className="text-sm font-mono break-words bg-surface-2 rounded-md px-2 py-1.5">{t.topic}</p>
        <div className="flex flex-wrap items-center gap-1.5">
          <ModeLabel mode={t.mode} variant="badge" />
          {t.council_id ? <Badge tone="info">Saved council</Badge> : <Badge>{t.provider} · {t.model}</Badge>}
          {t.variables.map((v) => <Badge key={v} tone="warning">{`{{${v}}}`}</Badge>)}
        </div>
        <div className="mt-auto flex items-center gap-2">
          <span className="text-xs text-fg-subtle">
            {t.use_count ? `Used ${t.use_count}×${t.last_used_at ? `, last ${relTime(t.last_used_at)}` : ""}` : "Never used"}
          </span>
          <Button className="ml-auto" size="sm" variant="primary" icon={Play} onClick={onRun}>Run</Button>
        </div>
      </Card>
    </li>
  );
}

export default function Templates() {
  const templates = useTemplates();
  const [running, setRunning] = useState<RunTemplate | null>(null);
  const [editing, setEditing] = useState<RunTemplate | null>(null);
  return (
    <Page>
      <PageHeader title="Templates" description="Saved run configurations. Variables in {{braces}} are filled in each time you run one."
        actions={<ButtonLink to="/" icon={Plus}>New from composer</ButtonLink>} />
      {templates.isLoading ? (
        <div className="grid gap-3 sm:grid-cols-2"><Skeleton className="h-40" /><Skeleton className="h-40" /></div>
      ) : templates.error ? (
        <Card as="div"><ErrorState error={templates.error} onRetry={() => void templates.refetch()} /></Card>
      ) : templates.data?.length ? (
        <ul className="grid gap-3 sm:grid-cols-2">
          {templates.data.map((t) => <TemplateCard key={t.id} t={t} onRun={() => setRunning(t)} onEdit={() => setEditing(t)} />)}
        </ul>
      ) : (
        <Card as="div">
          <EmptyState icon={LayoutTemplate} title="No templates yet"
            body="Set up a run on Home and choose “Save as template”. Add {{variables}} to the topic to reuse it for different subjects." />
        </Card>
      )}
      <RunTemplateDialog template={running} onOpenChange={(o) => !o && setRunning(null)} />
      <EditTemplateDialog template={editing} onOpenChange={(o) => !o && setEditing(null)} />
    </Page>
  );
}
