// Council detail header with inline editing of name, description, mode and seat bounds.

import { Pencil, Play, Trash2 } from "lucide-react";
import { useState, type FormEvent } from "react";

import { NumberField } from "@/components/councils/fields";
import { ModeBadge } from "@/components/councils/parts";
import { Button, ButtonLink, Callout, Card, FormActions, PageHeader, Segmented, TextArea, TextField } from "@/components/ui";
import { fieldErrorsFrom, MODES, validateBounds, validateName, type FieldErrors } from "@/features/councils/logic";
import { useUpdateCouncil, type CouncilPatch } from "@/features/councils/queries";
import { relTime } from "@/lib/format";
import type { Council, Mode } from "@/lib/types";
import { toast } from "@/stores/toast";

export function StartRunLink({ className, label = "Start run with this council", variant = "primary" }: {
  className?: string;
  label?: string;
  variant?: "primary" | "secondary";
}) {
  return <ButtonLink to="/" variant={variant} icon={Play} className={className}>{label}</ButtonLink>;
}

export function CouncilHeader({ council, onDelete }: { council: Council; onDelete: () => void }) {
  const [editing, setEditing] = useState(false);
  const seats = council.members.length;

  if (editing) return <HeaderForm council={council} onDone={() => setEditing(false)} />;
  return (
    <PageHeader title={council.name} badge={<ModeBadge mode={council.mode} />}
      description={<span className="whitespace-pre-line">{council.description || <span className="text-fg-subtle">No description.</span>}</span>}
      meta={
        <p className="text-xs text-fg-subtle tabular">
          {seats} seat{seats === 1 ? "" : "s"} (bounds {council.min_size}–{council.max_size}) · updated{" "}
          <time dateTime={council.updated_at}>{relTime(council.updated_at)}</time>
        </p>
      }
      actions={
        <>
          <Button icon={Pencil} onClick={() => setEditing(true)}>Edit details</Button>
          <Button icon={Trash2} variant="danger-ghost" onClick={onDelete}>Delete</Button>
          <StartRunLink label="Start run" />
        </>
      } />
  );
}

function HeaderForm({ council, onDone }: { council: Council; onDone: () => void }) {
  const update = useUpdateCouncil(council.id);
  const [name, setName] = useState(council.name);
  const [description, setDescription] = useState(council.description);
  const [mode, setMode] = useState<Mode>(council.mode);
  const [min, setMin] = useState(council.min_size);
  const [max, setMax] = useState(council.max_size);
  const [valid, setValid] = useState({ min: true, max: true });
  const [tried, setTried] = useState(false);
  const [server, setServer] = useState<FieldErrors>({});

  const seats = council.members.length;
  const client: FieldErrors = {};
  const ne = validateName(name);
  if (ne) client.name = ne;
  Object.assign(client, validateBounds(min, max));
  if (!client.min_size && !client.max_size && (seats < min || seats > max)) {
    client.size = `The council has ${seats} seats, outside ${min}–${max}. Resize it on the Seats tab first.`;
  }
  if (!valid.min || !valid.max) client._form = "Fix the seat bounds.";
  const errors = { ...(tried ? client : {}), ...server };

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setTried(true);
    setServer({});
    if (Object.keys(client).length) return;
    const patch: CouncilPatch = {};
    if (name.trim() !== council.name) patch.name = name.trim();
    if (description !== council.description) patch.description = description;
    if (mode !== council.mode) patch.mode = mode;
    if (min !== council.min_size) patch.min_size = min;
    if (max !== council.max_size) patch.max_size = max;
    if (!Object.keys(patch).length) return onDone();
    try {
      await update.mutateAsync(patch);
      toast({ tone: "success", title: "Council updated" });
      onDone();
    } catch (err) {
      setServer(fieldErrorsFrom(err));
    }
  };

  return (
    <Card className="p-5 mb-6">
      <form onSubmit={submit} noValidate className="flex flex-col gap-4" aria-labelledby="edit-council-h">
        {/* The page's h1 stays present while editing, so headings remain correct. */}
        <h1 id="edit-council-h" className="text-xl">Edit {council.name}</h1>
        <TextField label="Name" value={name} maxLength={80} error={errors.name} autoFocus
          onChange={(e) => { setName(e.target.value); setServer((s) => ({ ...s, name: "" })); }} />
        <TextArea label="Description" value={description} maxLength={4000} hint={`${description.length}/4000`}
          onChange={(e) => setDescription(e.target.value)} />
        <div className="flex flex-col gap-1">
          <Segmented label="Mode" value={mode} onChange={setMode} options={MODES} />
          <p className="text-xs text-fg-subtle">Mode decides which roles are added when you grow the council.</p>
        </div>
        <div className="grid grid-cols-2 gap-4 sm:max-w-sm">
          <NumberField label="Minimum seats" integer min={1} max={64} value={min} error={errors.min_size}
            onChange={(v) => v != null && setMin(v)} onValidityChange={(ok) => setValid((x) => ({ ...x, min: ok }))} />
          <NumberField label="Maximum seats" integer min={1} max={64} value={max} error={errors.max_size}
            onChange={(v) => v != null && setMax(v)} onValidityChange={(ok) => setValid((x) => ({ ...x, max: ok }))} />
        </div>
        {errors.size && <p className="text-xs text-danger" role="alert">{errors.size}</p>}
        {errors._form && <Callout tone="danger" title="Could not save">{errors._form}</Callout>}
        <FormActions cancel={{ onClick: onDone }}>
          <Button type="submit" variant="primary" loading={update.isPending}>Save details</Button>
        </FormActions>
      </form>
    </Card>
  );
}
