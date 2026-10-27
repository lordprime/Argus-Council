// Template UI shared by the Templates page and Home: the variables form that runs a template,
// the "Save as template" dialog, and the "Start from template" picker.

import { BookmarkPlus, LayoutTemplate, Play } from "lucide-react";
import { useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router";

import { Badge, Button, Callout, Dialog, EmptyState, ErrorState, Skeleton, TextArea, TextField } from "@/components/ui";
import { ApiError, errorMessage } from "@/lib/api";

import {
  fillTemplate,
  templateVariables,
  useCreateTemplate,
  useRunTemplate,
  useTemplates,
  variableLabel,
  type RunTemplate,
  type TemplateConfig,
} from "./queries";

/** Variables form: one field per {{variable}}, a live preview of the topic, and Run. */
export function RunTemplateForm({ template, onDone }: { template: RunTemplate; onDone?: () => void }) {
  const navigate = useNavigate();
  const run = useRunTemplate();
  const vars = template.variables.length ? template.variables : templateVariables(template.topic, template.description);
  const [values, setValues] = useState<Record<string, string>>({});
  const [touched, setTouched] = useState(false);
  const missing = vars.filter((v) => !values[v]?.trim());
  const serverMissing = run.error instanceof ApiError ? ((run.error.details.missing as string[] | undefined) ?? []) : [];

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setTouched(true);
    if (missing.length) return;
    try {
      const r = await run.mutateAsync({ id: template.id, variables: Object.fromEntries(vars.map((v) => [v, values[v]!.trim()])) });
      onDone?.();
      navigate(`/runs/${r.id}`);
    } catch {
      /* shown below */
    }
  };

  return (
    <form onSubmit={submit} className="flex flex-col gap-4" aria-label={`Run template ${template.name}`} noValidate>
      {vars.length === 0 && <p className="text-sm text-fg-muted">This template has no variables. It runs exactly as saved.</p>}
      {vars.map((v, i) => {
        const err = (touched && !values[v]?.trim()) || serverMissing.includes(v) ? `${variableLabel(v)} is required.` : undefined;
        return (
          <TextField key={v} label={variableLabel(v)} required autoFocus={i === 0} maxLength={500}
            value={values[v] ?? ""} error={err} hint={`{{${v}}}`}
            onChange={(e) => setValues((x) => ({ ...x, [v]: e.target.value }))} />
        );
      })}
      <div className="rounded-md bg-surface-2 p-3 text-sm">
        <p className="text-xs text-fg-subtle mb-1">Topic preview</p>
        <p className="break-words" data-testid="template-preview">{fillTemplate(template.topic, values)}</p>
      </div>
      {run.error && <Callout tone="danger" title="Could not start the run">{errorMessage(run.error)}</Callout>}
      <div className="flex justify-end">
        <Button type="submit" variant="primary" icon={Play} loading={run.isPending}>Start run</Button>
      </div>
    </form>
  );
}

export function RunTemplateDialog({ template, onOpenChange }: { template: RunTemplate | null; onOpenChange: (open: boolean) => void }) {
  return (
    <Dialog open={Boolean(template)} onOpenChange={onOpenChange} title={template ? `Run “${template.name}”` : "Run template"}
      description={template?.summary || "Fill in the variables to start a run."}>
      {template && <RunTemplateForm template={template} onDone={() => onOpenChange(false)} />}
    </Dialog>
  );
}

/** "Save as template" for the Home composer: `getConfig` reads the composer's current settings. */
export function SaveTemplateButton({ getConfig, disabled }: { getConfig: () => TemplateConfig; disabled?: boolean }) {
  const [config, setConfig] = useState<TemplateConfig | null>(null);
  const [name, setName] = useState("");
  const [summary, setSummary] = useState("");
  const create = useCreateTemplate();
  const vars = config ? templateVariables(config.topic, config.description) : [];
  const open = () => {
    create.reset();
    setConfig(getConfig());
    setName("");
    setSummary("");
  };
  const save = async (e: FormEvent) => {
    e.preventDefault();
    if (!config || !name.trim()) return;
    try {
      await create.mutateAsync({ ...config, name: name.trim(), summary });
      setConfig(null);
    } catch {
      /* shown below */
    }
  };
  return (
    <>
      <Button type="button" variant="ghost" icon={BookmarkPlus} disabled={disabled} onClick={open}>Save as template</Button>
      <Dialog open={Boolean(config)} onOpenChange={(o) => !o && setConfig(null)} title="Save as template"
        description="Reuse this run configuration later. Put {{variables}} in the topic or description to fill in at run time.">
        {config && (
          <form onSubmit={save} className="flex flex-col gap-4">
            <TextField label="Template name" required autoFocus maxLength={120} value={name} onChange={(e) => setName(e.target.value)} />
            <TextArea label="Notes" maxLength={500} value={summary} onChange={(e) => setSummary(e.target.value)}
              hint="Optional: when to use this template." />
            <div className="text-sm">
              <p className="text-xs text-fg-subtle">Topic</p>
              <p className="break-words">{config.topic}</p>
            </div>
            <div className="flex flex-wrap items-center gap-1.5 text-sm">
              <span className="text-xs text-fg-subtle">Variables:</span>
              {vars.length ? vars.map((v) => <Badge key={v}>{`{{${v}}}`}</Badge>)
                : <span className="text-xs text-fg-muted">none (add e.g. {"{{company}}"} to the topic)</span>}
            </div>
            {create.error && <Callout tone="danger" title="Could not save the template">{errorMessage(create.error)}</Callout>}
            <div className="flex justify-end gap-2">
              <Button type="button" onClick={() => setConfig(null)}>Cancel</Button>
              <Button type="submit" variant="primary" loading={create.isPending} disabled={!name.trim()}>Save template</Button>
            </div>
          </form>
        )}
      </Dialog>
    </>
  );
}

/** Home's "Start from template": pick a template, then fill its variables. */
export function TemplatePicker() {
  const [open, setOpen] = useState(false);
  const [chosen, setChosen] = useState<RunTemplate | null>(null);
  const templates = useTemplates();
  const close = (o: boolean) => {
    setOpen(o);
    if (!o) setChosen(null);
  };
  return (
    <>
      <Button icon={LayoutTemplate} onClick={() => setOpen(true)}>Start from template</Button>
      <Dialog open={open} onOpenChange={close} wide
        title={chosen ? `Run “${chosen.name}”` : "Start from template"}
        description={chosen ? chosen.summary || "Fill in the variables to start a run." : "Pick a saved run configuration."}>
        {chosen ? (
          <div className="flex flex-col gap-3">
            <Button size="sm" variant="ghost" className="self-start" onClick={() => setChosen(null)}>← All templates</Button>
            <RunTemplateForm template={chosen} onDone={() => close(false)} />
          </div>
        ) : templates.isLoading ? (
          <div className="space-y-2"><Skeleton className="h-12" /><Skeleton className="h-12" /></div>
        ) : templates.error ? (
          <ErrorState error={templates.error} onRetry={() => void templates.refetch()} />
        ) : templates.data?.length ? (
          <ul className="flex flex-col divide-y divide-border">
            {templates.data.map((t) => (
              <li key={t.id}>
                <button type="button" onClick={() => setChosen(t)}
                  className="w-full text-left px-2 py-2.5 rounded-md hover:bg-surface-2 flex flex-col gap-0.5">
                  <span className="flex items-center gap-2"><span className="font-medium">{t.name}</span><Badge>{t.mode}</Badge></span>
                  <span className="text-xs text-fg-muted truncate">{t.topic}</span>
                </button>
              </li>
            ))}
          </ul>
        ) : (
          <EmptyState icon={LayoutTemplate} title="No templates yet"
            body={<>Fill in the composer and choose <strong>Save as template</strong>, or see <Link to="/templates" className="text-accent underline" onClick={() => close(false)}>Templates</Link>.</>} />
        )}
      </Dialog>
    </>
  );
}
