import { ChevronDown, Users } from "lucide-react";
import { useMemo, useRef, useState, type FormEvent, type ReactNode } from "react";
import { useNavigate } from "react-router";

import { NumberField } from "@/components/councils/fields";
import { GuardrailsEditor, GuardrailsSummary } from "@/components/councils/GuardrailsEditor";
import { ModelPicker } from "@/components/councils/ModelPicker";
import { RoleChip } from "@/components/councils/parts";
import { draftKey, SeatDraftList, type SeatDraft } from "@/components/councils/SeatDraftList";
import { Button, Callout, Card, ErrorState, FormActions, Page, PageHeader, Segmented, Skeleton, Stepper, TextArea, TextField } from "@/components/ui";
import {
  clamp, DEFAULT_SIZE, fieldErrorsFrom, MODES, rosterFor, roleTitle, validateBounds, validateName,
  type Dict, type FieldErrors,
} from "@/features/councils/logic";
import { useCreateCouncil, type MemberInput } from "@/features/councils/queries";
import { useRoles } from "@/lib/queries";
import type { Mode } from "@/lib/types";

type SeatMode = "roster" | "custom";

function Section({ id, title, description, children }: { id: string; title: string; description?: string; children: ReactNode }) {
  return (
    <Card className="p-5 flex flex-col gap-5" aria-labelledby={id}>
      <div>
        <h2 id={id} className="text-lg">{title}</h2>
        {description && <p className="text-sm text-fg-muted mt-0.5">{description}</p>}
      </div>
      {children}
    </Card>
  );
}

export default function CouncilBuilder() {
  const navigate = useNavigate();
  const roles = useRoles();
  const create = useCreateCouncil();

  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [mode, setMode] = useState<Mode>("research");
  const [minSize, setMinSize] = useState(1);
  const [maxSize, setMaxSize] = useState(12);
  const [boundsValid, setBoundsValid] = useState({ min: true, max: true });
  const [seatMode, setSeatMode] = useState<SeatMode>("roster");
  const [size, setSize] = useState(DEFAULT_SIZE.research);
  const [seats, setSeats] = useState<SeatDraft[]>([]);
  const [route, setRoute] = useState({ provider: "mock", model: "mock-1" });
  const [guardrails, setGuardrails] = useState<Dict>({});
  const [guardrailsValid, setGuardrailsValid] = useState(true);
  const [showGuardrails, setShowGuardrails] = useState(false);
  const [submitted, setSubmitted] = useState(false);
  const [serverErrors, setServerErrors] = useState<FieldErrors>({});
  const summaryRef = useRef<HTMLDivElement>(null);

  const roleList = useMemo(() => roles.data?.roles ?? [], [roles.data]);
  const overseers = useMemo(() => new Set(roleList.filter((r) => r.overseer).map((r) => r.key)), [roleList]);
  const boundsOk = boundsValid.min && boundsValid.max && minSize <= maxSize;
  const lo = boundsOk ? minSize : 1;
  const hi = boundsOk ? maxSize : 64;
  const rosterSize = clamp(size, lo, hi);
  const roster = rosterFor(roles.data?.rosters, overseers, mode, rosterSize);

  const rosterDrafts = (m: Mode, n: number): SeatDraft[] => {
    const taken = new Map<string, number>();
    return rosterFor(roles.data?.rosters, overseers, m, n).map((role) => {
      const k = (taken.get(role) ?? 0) + 1;
      taken.set(role, k);
      const title = roleTitle(roleList, role);
      return { key: draftKey(), role, name: k === 1 ? title : `${title} ${k}` };
    });
  };

  // ------------------------------------------------------------ validation
  const clientErrors: FieldErrors = {};
  const nameErr = validateName(name);
  if (nameErr) clientErrors.name = nameErr;
  const b = validateBounds(minSize, maxSize);
  if (b.min_size) clientErrors.min_size = b.min_size;
  if (b.max_size) clientErrors.max_size = b.max_size;
  if (!boundsValid.min) clientErrors.min_size ??= "Enter a whole number from 1 to 64.";
  if (!boundsValid.max) clientErrors.max_size ??= "Enter a whole number from 1 to 64.";
  const count = seatMode === "roster" ? rosterSize : seats.length;
  if (boundsOk && (count < minSize || count > maxSize)) {
    clientErrors.size = `This council needs between ${minSize} and ${maxSize} seats; it has ${count}.`;
  }
  if (seatMode === "custom" && seats.some((s) => validateName(s.name))) clientErrors.members = "Fix the seat names below.";
  if (!route.model.trim()) clientErrors.model = "Enter a model id.";
  if (!guardrailsValid) clientErrors.guardrails = "Fix the highlighted guardrail values.";

  const errors: FieldErrors = { ...(submitted ? clientErrors : {}), ...serverErrors };
  const errorList = Object.entries(errors);

  const clearServer = (...ks: string[]) =>
    setServerErrors((e) => (ks.some((k) => k in e) ? Object.fromEntries(Object.entries(e).filter(([k]) => !ks.includes(k))) : e));

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setSubmitted(true);
    setServerErrors({});
    if (Object.keys(clientErrors).length) {
      if (!guardrailsValid) setShowGuardrails(true);
      requestAnimationFrame(() => summaryRef.current?.focus());
      return;
    }
    const members: MemberInput[] | undefined = seatMode === "custom"
      ? seats.map((s) => {
        const info = roleList.find((r) => r.key === s.role);
        return {
          name: s.name.trim(), role: s.role, provider: route.provider, model: route.model.trim(),
          system_prompt: "", temperature: 0.3, enabled: true,
          config: { tools: info?.default_tools ?? [], overseer: info?.overseer ?? false },
        };
      })
      : undefined;
    try {
      const council = await create.mutateAsync({
        name: name.trim(), description: description.trim(), mode, min_size: minSize, max_size: maxSize, guardrails,
        default_provider: route.provider, default_model: route.model.trim(),
        ...(members ? { members } : { size: rosterSize }),
      });
      navigate(`/councils/${council.id}`);
    } catch (err) {
      const fe = fieldErrorsFrom(err);
      if (fe.members && seatMode === "roster") {
        fe.size = fe.members;
        delete fe.members;
      }
      setServerErrors(fe);
      requestAnimationFrame(() => summaryRef.current?.focus());
    }
  };

  const LABELS: Record<string, string> = {
    name: "Name", description: "Description", mode: "Mode", min_size: "Minimum seats", max_size: "Maximum seats",
    size: "Seats", members: "Seats", model: "Default model", default_model: "Default model",
    default_provider: "Default provider", guardrails: "Guardrails", _form: "Council",
  };

  return (
    <Page width="form">
      <PageHeader title="New council"
        description="Choose a mode, who sits on the council and the limits it works within. You can change all of this later." />

      <form onSubmit={submit} noValidate className="flex flex-col gap-6" aria-label="New council">
        {errorList.length > 0 && (
          <div ref={summaryRef} tabIndex={-1} className="focus-visible:outline-none">
            <Callout tone="danger" title={`Fix ${errorList.length} problem${errorList.length === 1 ? "" : "s"} to create this council`}>
              <ul className="list-disc pl-5">
                {errorList.map(([k, msg]) => <li key={k}><span className="font-medium">{LABELS[k] ?? k}:</span> {msg}</li>)}
              </ul>
            </Callout>
          </div>
        )}

        <Section id="basics-h" title="Basics">
          <TextField label="Name" required maxLength={80} autoFocus placeholder="e.g. Market research team" value={name}
            error={errors.name} hint="Letters, numbers, spaces, dots, dashes and underscores."
            onChange={(e) => { setName(e.target.value); clearServer("name"); }} />
          <TextArea label="Description" maxLength={4000} placeholder="What this council is for (optional)"
            hint={`${description.length}/4000`} value={description} onChange={(e) => setDescription(e.target.value)} />
          <div className="flex flex-col gap-1">
            <Segmented label="Mode" value={mode} options={MODES}
              onChange={(m) => {
                setMode(m);
                setSize(clamp(DEFAULT_SIZE[m], lo, hi));
              }} />
            <p className="text-xs text-fg-subtle">{MODES.find((m) => m.value === mode)?.hint} The mode picks the default roster.</p>
          </div>
        </Section>

        <Section id="seats-h" title="Seats" description="Each seat is an agent with a role. Overseers (dashed ring) review the others' work.">
          <div className="grid gap-4 grid-cols-2 sm:max-w-sm">
            <NumberField label="Minimum seats" integer min={1} max={64} value={minSize} error={errors.min_size}
              onValidityChange={(ok) => setBoundsValid((v) => ({ ...v, min: ok }))}
              onChange={(v) => { if (v != null) { setMinSize(v); if (v <= maxSize) setSize((s) => clamp(s, v, maxSize)); } clearServer("min_size", "size"); }} />
            <NumberField label="Maximum seats" integer min={1} max={64} value={maxSize} error={errors.max_size}
              onValidityChange={(ok) => setBoundsValid((v) => ({ ...v, max: ok }))}
              onChange={(v) => { if (v != null) { setMaxSize(v); if (minSize <= v) setSize((s) => clamp(s, minSize, v)); } clearServer("max_size", "size"); }} />
          </div>

          <Segmented label="How to fill the seats" value={seatMode}
            options={[
              { value: "roster", label: "Start from roster", hint: "Seat the default roles for the mode." },
              { value: "custom", label: "Pick seats", hint: "Choose each seat's role yourself." },
            ]}
            onChange={(v) => {
              setSeatMode(v);
              if (v === "custom" && seats.length === 0 && roles.data) setSeats(rosterDrafts(mode, rosterSize));
            }} />

          {roles.isLoading ? (
            <div className="flex flex-col gap-2" aria-hidden>
              <Skeleton className="h-9 w-40" />
              <div className="flex gap-2"><Skeleton className="h-7 w-28" /><Skeleton className="h-7 w-28" /><Skeleton className="h-7 w-28" /></div>
            </div>
          ) : roles.error ? (
            <ErrorState error={roles.error} onRetry={() => void roles.refetch()} />
          ) : seatMode === "roster" ? (
            <div className="flex flex-col gap-3">
              <Stepper label="Council size" value={rosterSize} min={lo} max={hi} error={errors.size}
                hint={`Between ${lo} and ${hi} seats.`} onChange={(v) => { setSize(v); clearServer("size"); }} />
              <div>
                <p className="text-sm font-medium mb-2" id="roster-preview-h">Roster preview</p>
                <ul className="flex flex-wrap gap-1.5" aria-labelledby="roster-preview-h">
                  {roster.map((r, i) => <RoleChip key={`${r}-${i}`} role={r} roles={roleList} overseer={overseers.has(r)} />)}
                </ul>
              </div>
            </div>
          ) : (
            <div className="flex flex-col gap-2">
              {errors.size && <p className="text-xs text-danger" role="alert">{errors.size}</p>}
              <SeatDraftList seats={seats} roles={roleList} max={hi} showErrors={submitted}
                onChange={(s) => { setSeats(s); clearServer("size", "members"); }}
                onSeatDefaults={() => setSeats(rosterDrafts(mode, clamp(DEFAULT_SIZE[mode], lo, hi)))}
                defaultsLabel={`Seat default roster for ${MODES.find((m) => m.value === mode)?.label.toLowerCase()}`} />
            </div>
          )}
        </Section>

        <Section id="model-h" title="Default model" description="Every seat starts on this model. You can change individual seats after creating the council.">
          <ModelPicker provider={route.provider} model={route.model} providerLabel="Default provider" modelLabel="Default model"
            errors={{ model: errors.model ?? errors.default_model, provider: errors.default_provider }}
            onChange={(r) => { setRoute(r); clearServer("default_model", "default_provider"); }} />
        </Section>

        <Card className="p-5 flex flex-col gap-4" aria-labelledby="guardrails-h">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <h2 id="guardrails-h" className="text-lg">Guardrails</h2>
              <p className="text-sm text-fg-muted mt-0.5">Budget, step limits, tool access and how firmly overseers step in.</p>
            </div>
            <Button type="button" variant="ghost" aria-expanded={showGuardrails} aria-controls="guardrails-panel"
              onClick={() => setShowGuardrails((s) => !s)}>
              <ChevronDown className={`size-4 transition-transform duration-fast ${showGuardrails ? "rotate-180" : ""}`} aria-hidden />
              {showGuardrails ? "Hide guardrails" : "Customize guardrails"}
            </Button>
          </div>
          {!showGuardrails && <GuardrailsSummary value={guardrails} />}
          {errors.guardrails && <p className="text-xs text-danger" role="alert">{errors.guardrails}</p>}
          <div id="guardrails-panel" hidden={!showGuardrails}>
            <GuardrailsEditor value={guardrails} onChange={(g) => { setGuardrails(g); clearServer("guardrails"); }}
              onValidChange={setGuardrailsValid} />
          </div>
        </Card>

        <FormActions size="lg" cancel={{ to: "/councils" }}
          meta={<>{seatMode === "roster" ? rosterSize : seats.length} seats · {route.provider}/{route.model || "?"}</>}>
          <Button type="submit" variant="primary" size="lg" icon={Users} loading={create.isPending}>Create council</Button>
        </FormActions>
      </form>
    </Page>
  );
}
