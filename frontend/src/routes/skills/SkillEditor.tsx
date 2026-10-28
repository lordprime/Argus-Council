import { AlertTriangle, CheckCircle2, Plus, Save, ShieldCheck, Trash2, XCircle } from "lucide-react";
import { useMemo, useRef, useState, type FormEvent } from "react";
import { Link, useNavigate, useSearchParams } from "react-router";

import { VersionHistory } from "@/components/skills/VersionHistory";
import {
  Button, Callout, Card, Chip, ErrorState, FormActions, IconButton, Page, PageHeader, Segmented, Skeleton, TextArea, TextField,
} from "@/components/ui";
import { Markdown } from "@/components/ui/Markdown";
import { useOptionalSkill, useSaveSkill, useValidateSkill, type ValidateResult } from "@/features/skills/queries";
import {
  composeSkillMd,
  demoteHeadings,
  fieldErrors,
  filePathError,
  frontmatterChecks,
  type FrontmatterCheck,
  KNOWN_TOOLS,
  MAX_DESCRIPTION,
  parseToolList,
  SKILL_PROSE,
  stripFrontmatter,
  type SkillFields,
} from "@/features/skills/skillmd";
import { ApiError, errorMessage } from "@/lib/api";
import { cn } from "@/lib/cn";
import type { SkillDetail } from "@/lib/types";
import { toast } from "@/stores/toast";

interface FileRow {
  id: number;
  path: string;
  content: string;
}

interface Initial {
  fields: SkillFields;
  files: FileRow[];
  enabled: boolean;
}

let nextId = 1;
const rowsFrom = (files: Record<string, string>): FileRow[] =>
  Object.entries(files).sort(([a], [b]) => a.localeCompare(b)).map(([path, content]) => ({ id: nextId++, path, content }));

function initialFrom(s: SkillDetail | undefined): Initial {
  if (!s) return { fields: { name: "", description: "", version: "1.0.0", allowedTools: [], body: "" }, files: [], enabled: true };
  return {
    fields: { name: s.name, description: s.description, version: s.version, allowedTools: s.allowed_tools, body: stripFrontmatter(s.skill_md) },
    files: rowsFrom(s.file_contents),
    enabled: s.builtin ? true : s.enabled,
  };
}

const CHECK_ICON = { ok: CheckCircle2, warn: AlertTriangle, error: XCircle } as const;
const CHECK_TONE = { ok: "text-success", warn: "text-warning", error: "text-danger" } as const;
const CHECK_SR = { ok: "OK", warn: "Suggestion", error: "Error" } as const;

/** Live frontmatter feedback: updates as the author types, before any save or server round-trip. */
function FrontmatterStatus({ checks }: { checks: FrontmatterCheck[] }) {
  const errors = checks.filter((c) => c.status === "error").length;
  return (
    <section aria-labelledby="fm-check-h" className="rounded-md border border-border bg-surface-2 p-3">
      <h3 id="fm-check-h" className="text-sm font-medium mb-2">
        Frontmatter check{" "}
        <span className={errors ? "text-danger" : "text-success"}>{errors ? `· ${errors} ${errors === 1 ? "error" : "errors"}` : "· looks valid"}</span>
      </h3>
      <ul className="flex flex-col gap-1.5 text-sm">
        {checks.map((c) => {
          const Icon = CHECK_ICON[c.status];
          return (
            <li key={c.field} className="flex items-start gap-2 min-w-0">
              <Icon className={cn("size-4 mt-0.5 shrink-0", CHECK_TONE[c.status])} aria-hidden />
              <span className="min-w-0 break-words">
                <span className="sr-only">{CHECK_SR[c.status]}: </span>
                <code className="font-mono text-xs">{c.field}</code> <span className="text-fg-muted">{c.message}</span>
              </span>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

// ------------------------------------------------------------------ form
function EditorForm({ mode, initial, sourceName }: { mode: "new" | "edit" | "customize"; initial: Initial; sourceName?: string }) {
  const navigate = useNavigate();
  const save = useSaveSkill();
  const validate = useValidateSkill();
  const summaryRef = useRef<HTMLDivElement>(null);

  const [name, setName] = useState(initial.fields.name);
  const [description, setDescription] = useState(initial.fields.description);
  const [version, setVersion] = useState(initial.fields.version);
  const [known, setKnown] = useState<string[]>(initial.fields.allowedTools.filter((t) => KNOWN_TOOLS.includes(t)));
  const [other, setOther] = useState(initial.fields.allowedTools.filter((t) => !KNOWN_TOOLS.includes(t)).join(", "));
  const [body, setBody] = useState(initial.fields.body);
  const [files, setFiles] = useState<FileRow[]>(initial.files);
  const [submitted, setSubmitted] = useState(false);
  const [serverResult, setServerResult] = useState<ValidateResult | null>(null);
  const [nameTaken, setNameTaken] = useState<string | null>(null);
  const [previewMode, setPreviewMode] = useState<"rendered" | "source">("rendered");

  const fields: SkillFields = { name, description, version, allowedTools: [...known, ...parseToolList(other)], body };
  const skillMd = composeSkillMd(fields);
  const fileDict = Object.fromEntries(files.map((f) => [f.path.trim(), f.content]));
  const payload = { skill_md: skillMd, files: fileDict, enabled: initial.enabled };

  const errs = fieldErrors(fields);
  const checks = frontmatterChecks(fields);
  if (nameTaken && nameTaken === name.trim()) errs.name = `You already have a skill named ${nameTaken}. Edit it instead, or pick another name.`;
  const fileErrs = Object.fromEntries(files.map((f) => {
    const dup = files.filter((g) => g.path.trim() === f.path.trim()).length > 1;
    return [f.id, filePathError(f.path) ?? (dup ? "Duplicate path" : undefined)];
  }));
  const allErrors = [
    ...Object.entries(errs).map(([k, v]) => `${k === "body" ? "Instructions" : k[0]!.toUpperCase() + k.slice(1)}: ${v}`),
    ...files.flatMap((f) => (fileErrs[f.id] ? [`File ${f.path || "(no path)"}: ${fileErrs[f.id]}`] : [])),
  ];
  const show = (k: keyof SkillFields) => (submitted ? errs[k] : undefined);
  // Preview sits under h2 "Instructions" > h3 "Preview", so SKILL.md "#" becomes h4.
  const preview = useMemo(() => demoteHeadings(body, 3), [body]);

  const runValidate = () => {
    setServerResult(null);
    validate.mutate(payload, { onSuccess: setServerResult });
  };

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setSubmitted(true);
    setServerResult(null);
    if (allErrors.length) {
      requestAnimationFrame(() => summaryRef.current?.focus());
      return;
    }
    try {
      const saved = await save.mutateAsync({ name: mode === "edit" ? initial.fields.name : undefined, body: payload });
      toast({ tone: "success", title: mode === "edit" ? `Saved ${saved.name}` : `Created ${saved.name}`,
        body: saved.shadows_builtin ? "It overrides the built-in skill of the same name." : undefined });
      navigate(`/skills/${encodeURIComponent(saved.name)}`);
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) setNameTaken(name.trim());
      requestAnimationFrame(() => summaryRef.current?.focus());
    }
  };

  const toggleTool = (t: string) => setKnown((k) => (k.includes(t) ? k.filter((x) => x !== t) : [...k, t]));
  const setFile = (id: number, patch: Partial<FileRow>) => setFiles((fs) => fs.map((f) => (f.id === id ? { ...f, ...patch } : f)));

  return (
    <form onSubmit={submit} noValidate className="flex flex-col gap-6">
      {((submitted && allErrors.length > 0) || (save.error && !(save.error instanceof ApiError && save.error.status === 409))) && (
        <div ref={summaryRef} tabIndex={-1} className="focus-visible:outline-none">
          <Callout tone="danger" title={save.error && !allErrors.length ? "Could not save the skill" : `Fix ${allErrors.length} ${allErrors.length === 1 ? "problem" : "problems"} before saving`}>
            {allErrors.length ? (
              <ul className="list-disc pl-5">{allErrors.map((m) => <li key={m}>{m}</li>)}</ul>
            ) : errorMessage(save.error)}
          </Callout>
        </div>
      )}

      <Card className="p-5 flex flex-col gap-4" aria-labelledby="meta-h">
        <h2 id="meta-h" className="text-lg">Frontmatter</h2>
        <div className="grid gap-4 sm:grid-cols-[minmax(0,1fr)_10rem]">
          <TextField label="Name" required value={name} onChange={(e) => setName(e.target.value.toLowerCase())} disabled={mode === "edit"}
            maxLength={64} spellCheck={false} autoComplete="off" error={show("name") ?? (nameTaken === name.trim() ? errs.name : undefined)}
            hint={mode === "edit" ? "The name cannot change. Create a new skill to rename." : mode === "customize"
              ? `Keep “${sourceName}” to override the built-in, or choose a new name for a separate skill.`
              : "Lowercase letters, digits and hyphens, e.g. api-design-review"} />
          <TextField label="Version" value={version} onChange={(e) => setVersion(e.target.value)} maxLength={32} spellCheck={false} placeholder="1.0.0" />
        </div>
        <TextArea label="Description" required value={description} onChange={(e) => setDescription(e.target.value)} rows={3}
          error={show("description")}
          hint={`${description.trim().length}/${MAX_DESCRIPTION}. Say what the skill does and when agents should use it; agents always see this.`} />
        <fieldset className="flex flex-col gap-2">
          <legend className="text-sm font-medium mb-1">Allowed tools</legend>
          <p className="text-xs text-fg-subtle -mt-1">Tools the skill expects to use. Run guardrails still decide what is permitted.</p>
          <div className="flex flex-wrap gap-1.5">
            {KNOWN_TOOLS.map((t) => {
              const on = known.includes(t);
              return (
                <Chip key={t} mono pressed={on} onPressedChange={() => toggleTool(t)}>{t}</Chip>
              );
            })}
          </div>
          <TextField label="Other tools" value={other} onChange={(e) => setOther(e.target.value)} spellCheck={false}
            placeholder="github.create_issue, search.*" hint="Comma-separated, e.g. tools from your MCP servers." />
        </fieldset>
        <FrontmatterStatus checks={checks} />
      </Card>

      <Card className="p-5 flex flex-col gap-3" aria-labelledby="body-h">
        <div>
          <h2 id="body-h" className="text-lg">Instructions</h2>
          <p className="text-sm text-fg-muted">Markdown. Agents read this when they call use_skill.</p>
        </div>
        <div className="grid gap-4 lg:grid-cols-2">
          <TextArea label="SKILL.md body" value={body} onChange={(e) => setBody(e.target.value)} rows={20}
            className="[&_textarea]:font-mono [&_textarea]:text-sm [&_textarea]:min-h-80" error={show("body")} spellCheck={false}
            placeholder={"## When to use\n\n## Steps\n1. …\n\n## Output\n…"} />
          <section aria-labelledby="preview-h" className="flex flex-col gap-1 min-w-0">
            <div className="flex flex-wrap items-end justify-between gap-2">
              <h3 id="preview-h" className="text-sm font-medium">Live SKILL.md preview</h3>
              <Segmented label="Preview as" value={previewMode} onChange={setPreviewMode} options={[
                { value: "rendered", label: "Rendered" },
                { value: "source", label: "SKILL.md" },
              ]} />
            </div>
            {previewMode === "rendered" ? (
              <div tabIndex={0} role="region" aria-label="Instructions preview"
                className="flex-1 min-h-80 max-h-[36rem] overflow-auto rounded-md border border-border bg-surface-2 px-4 py-2">
                {body.trim() ? <Markdown className={SKILL_PROSE} headingOffset={0}>{preview}</Markdown> : <p className="text-sm text-fg-subtle py-2">Nothing to preview yet.</p>}
              </div>
            ) : (
              <pre tabIndex={0} role="region" aria-label="SKILL.md source preview"
                className="flex-1 min-h-80 max-h-[36rem] overflow-auto rounded-md border border-border bg-surface-2 px-4 py-2 font-mono text-xs leading-5 whitespace-pre-wrap break-words">{skillMd}</pre>
            )}
          </section>
        </div>
      </Card>

      <Card className="p-5 flex flex-col gap-3" aria-labelledby="files-h">
        <div className="flex flex-wrap items-end justify-between gap-2">
          <div>
            <h2 id="files-h" className="text-lg">Bundled files</h2>
            <p className="text-sm text-fg-muted">Text resources the instructions refer to (references, templates, scripts). Up to 64 files, 512 KiB each.</p>
          </div>
          <Button type="button" icon={Plus} size="sm" onClick={() => setFiles((f) => [...f, { id: nextId++, path: "", content: "" }])}>Add file</Button>
        </div>
        {files.length === 0 && <p className="text-sm text-fg-subtle">No extra files.</p>}
        <ul className="flex flex-col gap-4">
          {files.map((f, i) => (
            <li key={f.id} className="rounded-md border border-border p-3 flex flex-col gap-2">
              <div className="flex items-start gap-2">
                <TextField label={`File ${i + 1} path`} value={f.path} onChange={(e) => setFile(f.id, { path: e.target.value })}
                  placeholder="references/checklist.md" className="flex-1" spellCheck={false}
                  error={submitted || f.path ? fileErrs[f.id] : undefined} />
                <IconButton type="button" label={`Remove file ${f.path || i + 1}`} icon={Trash2} variant="danger-ghost" className="mt-6"
                  onClick={() => setFiles((fs) => fs.filter((x) => x.id !== f.id))} />
              </div>
              <TextArea label={`File ${i + 1} content`} value={f.content} onChange={(e) => setFile(f.id, { content: e.target.value })}
                rows={6} spellCheck={false} className="[&_textarea]:font-mono [&_textarea]:text-xs" />
            </li>
          ))}
        </ul>
      </Card>

      <details className="rounded-lg border border-border bg-surface">
        <summary className="cursor-pointer px-4 py-3 text-sm font-medium">Generated SKILL.md</summary>
        <pre tabIndex={0} role="region" aria-label="Generated SKILL.md"
          className="px-4 pb-4 overflow-auto max-h-96 font-mono text-xs leading-5 whitespace-pre-wrap break-words">{skillMd}</pre>
      </details>

      <div aria-live="polite">
        {validate.error && <Callout tone="danger" title="Validation request failed">{errorMessage(validate.error)}</Callout>}
        {serverResult && (serverResult.valid ? (
          <Callout tone="success" title="Valid skill package">
            {serverResult.skill?.name} v{serverResult.skill?.version} · {serverResult.skill?.allowed_tools.length ?? 0} tools · {serverResult.skill?.files.length ?? 0} files
          </Callout>
        ) : (
          <Callout tone="danger" title="The server rejected this package">
            <ul className="list-disc pl-5">{serverResult.errors.map((m) => <li key={m}>{m}</li>)}</ul>
          </Callout>
        ))}
      </div>

      <FormActions sticky cancel={{ to: mode === "new" ? "/skills" : `/skills/${encodeURIComponent(sourceName ?? "")}` }}
        meta={serverResult?.valid && <span className="inline-flex items-center gap-1 text-success"><CheckCircle2 className="size-4" aria-hidden />Last validation passed</span>}>
        <Button type="submit" variant="primary" icon={Save} loading={save.isPending}>
          {mode === "edit" ? "Save changes" : mode === "customize" ? "Create my copy" : "Create skill"}
        </Button>
        <Button type="button" icon={ShieldCheck} loading={validate.isPending} onClick={runValidate}>Test with validate</Button>
      </FormActions>
    </form>
  );
}

// ------------------------------------------------------------------ page
export default function SkillEditor() {
  const [params] = useSearchParams();
  const editName = params.get("edit");
  const fromName = params.get("from");
  const source = editName ?? fromName;
  const q = useOptionalSkill(source);
  const [rev, setRev] = useState(0); // bumps after a restore so the form re-initialises from the restored content
  const mode: "new" | "edit" | "customize" = editName ? "edit" : fromName ? "customize" : "new";
  const title = mode === "edit" ? `Edit ${editName}` : mode === "customize" ? `Customize ${fromName}` : "New skill";

  let content;
  if (source && q.isLoading) {
    content = (
      <div role="status" aria-busy="true"><span className="sr-only">Loading skill…</span>
        <div aria-hidden className="space-y-4"><Skeleton className="h-48" /><Skeleton className="h-96" /></div>
      </div>
    );
  } else if (source && (q.error || !q.data)) {
    content = <Card><ErrorState error={q.error} onRetry={() => void q.refetch()} /></Card>;
  } else if (mode === "edit" && q.data?.builtin) {
    content = (
      <Callout tone="info" title="Built-in skills are read-only"
        action={<Link to={`/skills/new?from=${encodeURIComponent(q.data.name)}`} className="text-sm text-accent underline self-center">Customize instead</Link>}>
        Create your own copy with the same name to override it.
      </Callout>
    );
  } else {
    content = (
      <div className="flex flex-col gap-6">
        <EditorForm key={`${source ?? "new"}-${rev}`} mode={mode} initial={initialFrom(q.data)} sourceName={source ?? undefined} />
        {mode === "edit" && q.data && (
          <VersionHistory name={q.data.name} current={{ skill_md: q.data.skill_md, files: q.data.file_contents }}
            onRestored={() => setRev((r) => r + 1)} />
        )}
      </div>
    );
  }

  return (
    <Page>
      <PageHeader title={title}
        description={mode === "customize"
          ? "Your copy starts from the built-in. Saving it under the same name overrides the built-in for your councils."
          : "A skill is a SKILL.md (frontmatter plus instructions) and optional text files."} />
      {content}
    </Page>
  );
}
