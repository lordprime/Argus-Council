// Seats tab building blocks: seat card, seat editor dialog and the resize panel with preview.

import { ArrowDown, ArrowUp, Eye, Pencil, Trash2 } from "lucide-react";
import { useState } from "react";

import { FallbackChain, type Route } from "@/components/councils/FallbackChain";
import { MiniSwitch, NumberField, TagInput } from "@/components/councils/fields";
import { AuthBadge, ModelPicker } from "@/components/councils/ModelPicker";
import { isOverseer, RoleChip } from "@/components/councils/parts";
import { Badge, Button, CardTitle, Dialog, IconButton, SelectField, Stepper, TextArea, TextField } from "@/components/ui";
import { RoleAvatar, roleStyle } from "@/components/ui/role";
import { CLI_PROVIDERS, isCliProvider, useCliSessions } from "@/features/connections/cliAgents";
import { deepEqual, roleTitle, validateName, type ResizePreview } from "@/features/councils/logic";
import type { MemberInput, MemberPatch } from "@/features/councils/queries";
import { cn } from "@/lib/cn";
import type { Member, RoleInfo } from "@/lib/types";

/** The seat's fallback routes from its config (ignores malformed entries). */
export function seatFallbacks(config: Member["config"] | undefined): Route[] {
  const raw = config?.fallbacks;
  if (!Array.isArray(raw)) return [];
  return raw.flatMap((f: unknown) => {
    if (!f || typeof f !== "object") return [];
    const r = f as { provider?: unknown; model?: unknown };
    return typeof r.provider === "string" ? [{ provider: r.provider, model: typeof r.model === "string" ? r.model : "" }] : [];
  });
}

/** Pick an existing CLI session of yours to resume on this seat (validated server-side at run time). */
function CliSessionPicker({ provider, value, onChange }: { provider: string; value: string; onChange: (v: string) => void }) {
  const cli = CLI_PROVIDERS[provider];
  const q = useCliSessions(cli, Boolean(cli));
  const rows = q.data ?? [];
  return (
    <SelectField label="CLI session" value={value} disabled={q.isLoading} onChange={(e) => onChange(e.target.value)}
      hint="Default: a fresh vendor session per run, kept across this seat's turns. Or continue one of your earlier sessions.">
      <option value="">New session for each run</option>
      {value && !rows.some((r) => r.session_id === value) && <option value={value}>{value}</option>}
      {rows.map((r) => (
        <option key={r.id} value={r.session_id}>
          {r.label ? `${r.label} · ` : ""}{r.session_id.slice(0, 8)}… ({new Date(r.last_used_at).toLocaleDateString()})
        </option>
      ))}
    </SelectField>
  );
}

// ------------------------------------------------------------------ Seat card
export function SeatCard({ member, index, count, roles, canRemove, removeReason, pendingRemoval, busy, onToggle, onMove, onEdit, onRemove }: {
  member: Member;
  index: number;
  count: number;
  roles?: RoleInfo[];
  canRemove: boolean;
  removeReason?: string;
  pendingRemoval?: boolean;
  busy?: boolean;
  onToggle: (enabled: boolean) => void;
  onMove: (dir: "up" | "down") => void;
  onEdit: () => void;
  onRemove: () => void;
}) {
  const overseer = isOverseer(member);
  const tools = member.config?.tools ?? [];
  const fallbacks = seatFallbacks(member.config);
  const s = roleStyle(member.role);
  return (
    <li className={cn("flex flex-wrap md:flex-nowrap items-center gap-x-3 gap-y-2 rounded-lg border border-border border-l-[3px] bg-surface shadow-1 py-3 pl-3 pr-2",
      s.border, !member.enabled && "bg-surface-2", pendingRemoval && "border-dashed border-danger opacity-70")}
      aria-label={`${member.name}, seat ${index + 1} of ${count}`}>
      <span className="w-5 text-right text-xs text-fg-subtle tabular" aria-hidden>{index + 1}</span>
      <RoleAvatar role={member.role} overseer={overseer} size={36}
        label={`${roleTitle(roles, member.role)}${overseer ? " (overseer)" : ""}`} />
      <div className="min-w-0 flex-1 basis-48">
        <p className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <CardTitle as="span" className={cn("truncate", !member.enabled && "text-fg-muted")}>{member.name}</CardTitle>
          {member.name !== roleTitle(roles, member.role) && <span className={cn("text-sm", s.fg)}>{roleTitle(roles, member.role)}</span>}
          {overseer && <Badge tone="warning"><Eye className="size-3" aria-hidden />Overseer</Badge>}
          {isCliProvider(member.provider) && <AuthBadge auth="account" />}
          {!member.enabled && <Badge>Disabled</Badge>}
          {pendingRemoval && <Badge tone="danger">Will be removed</Badge>}
        </p>
        <p className="text-xs text-fg-subtle mt-0.5 flex flex-wrap gap-x-2">
          <span className="font-mono truncate max-w-full">{member.provider}/{member.model}</span>
          <span className="tabular">temp {member.temperature ?? "—"}</span>
          <span>{tools.length ? `${tools.length} tool${tools.length === 1 ? "" : "s"}` : "no tools"}</span>
          {fallbacks.length > 0 && <span>then {fallbacks.map((f) => f.provider).join(" → ")}</span>}
        </p>
      </div>
      <div className="flex items-center gap-0.5 ml-auto">
        <MiniSwitch checked={member.enabled} onChange={onToggle} label={`${member.name} enabled`} disabled={busy} />
        <IconButton label={`Move ${member.name} up`} icon={ArrowUp} disabled={index === 0 || busy}
          data-move={`${member.id}-up`} onClick={() => onMove("up")} />
        <IconButton label={`Move ${member.name} down`} icon={ArrowDown} disabled={index === count - 1 || busy}
          data-move={`${member.id}-down`} onClick={() => onMove("down")} />
        <IconButton label={`Edit ${member.name}`} icon={Pencil} onClick={onEdit} />
        <IconButton label={canRemove ? `Remove ${member.name}` : `Remove ${member.name} (${removeReason})`} icon={Trash2}
          variant="danger-ghost" disabled={!canRemove} onClick={onRemove} />
      </div>
    </li>
  );
}

// ------------------------------------------------------------------ Seat editor dialog
export function SeatEditDialog({ open, onOpenChange, member, roles, defaults, onSave, pending, error }: {
  open: boolean;
  onOpenChange: (o: boolean) => void;
  /** Undefined to add a new seat. */
  member?: Member;
  roles: RoleInfo[];
  defaults: { provider: string; model: string; name: (role: string) => string };
  onSave: (body: MemberPatch | MemberInput) => void;
  pending?: boolean;
  error?: string;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange} wide
      title={member ? `Edit ${member.name}` : "Add seat"}
      description={member ? "Changes apply to the next run of this council." : "The new seat joins at the end of the council."}>
      {open && <SeatForm key={member?.id ?? "new"} member={member} roles={roles} defaults={defaults} onSave={onSave}
        onCancel={() => onOpenChange(false)} pending={pending} error={error} />}
    </Dialog>
  );
}

function SeatForm({ member, roles, defaults, onSave, onCancel, pending, error }: {
  member?: Member;
  roles: RoleInfo[];
  defaults: { provider: string; model: string; name: (role: string) => string };
  onSave: (body: MemberPatch | MemberInput) => void;
  onCancel: () => void;
  pending?: boolean;
  error?: string;
}) {
  const initialRole = member?.role ?? "assistant";
  const roleInfo = (k: string) => roles.find((r) => r.key === k);
  const [name, setName] = useState(member?.name ?? defaults.name(initialRole));
  const [role, setRole] = useState(initialRole);
  const [route, setRoute] = useState({ provider: member?.provider ?? defaults.provider, model: member?.model ?? defaults.model });
  const [temperature, setTemperature] = useState<number>(member?.temperature ?? 0.3);
  const [prompt, setPrompt] = useState(member?.system_prompt ?? "");
  const [tools, setTools] = useState<string[]>(member?.config?.tools ?? roleInfo(initialRole)?.default_tools ?? []);
  const [fallbacks, setFallbacks] = useState<Route[]>(seatFallbacks(member?.config));
  const [cliSession, setCliSession] = useState<string>(typeof member?.config?.cli_session === "string" ? member.config.cli_session : "");
  const [tempValid, setTempValid] = useState(true);
  const [tried, setTried] = useState(false);

  const nameErr = validateName(name);
  const modelErr = route.model.trim() ? undefined : "Enter a model id.";
  const fallbackErr = fallbacks.some((f) => !f.model.trim()) ? "Every fallback needs a model id." : undefined;
  const roleDefaults = roleInfo(role)?.default_tools ?? [];

  const changeRole = (next: string) => {
    // Follow the role's default tools unless the user customized them.
    if (deepEqual([...tools].sort(), [...(roleInfo(role)?.default_tools ?? [])].sort())) setTools(roleInfo(next)?.default_tools ?? []);
    if (!member && name === defaults.name(role)) setName(defaults.name(next));
    setRole(next);
  };

  const submit = () => {
    setTried(true);
    if (nameErr || modelErr || fallbackErr || !tempValid) return;
    const config: Member["config"] = { ...(member?.config ?? {}), tools, overseer: roleInfo(role)?.overseer ?? false };
    delete config.fallbacks;
    delete config.cli_session;
    if (fallbacks.length) config.fallbacks = fallbacks.map((f) => ({ provider: f.provider, model: f.model.trim() }));
    if (isCliProvider(route.provider) && cliSession) config.cli_session = cliSession;
    if (!member) {
      onSave({ name: name.trim(), role, provider: route.provider, model: route.model.trim(), system_prompt: prompt,
        temperature, enabled: true, config } satisfies MemberInput);
      return;
    }
    const patch: MemberPatch = {};
    if (name.trim() !== member.name) patch.name = name.trim();
    if (role !== member.role) patch.role = role;
    if (route.provider !== member.provider) patch.provider = route.provider;
    if (route.model.trim() !== member.model) patch.model = route.model.trim();
    if (temperature !== member.temperature) patch.temperature = temperature;
    if (prompt !== member.system_prompt) patch.system_prompt = prompt;
    if (!deepEqual(config, member.config)) patch.config = config;
    onSave(patch);
  };

  return (
    <form className="flex flex-col gap-5" noValidate onSubmit={(e) => { e.preventDefault(); submit(); }}>
      <div className="grid gap-4 sm:grid-cols-2">
        <TextField label="Name" value={name} maxLength={80} error={tried ? nameErr : undefined} autoFocus
          onChange={(e) => setName(e.target.value)} />
        <SelectField label="Role" value={role} onChange={(e) => changeRole(e.target.value)}
          hint={roleInfo(role)?.description}>
          {roles.map((r) => <option key={r.key} value={r.key}>{r.title}{r.overseer ? " (overseer)" : ""}</option>)}
        </SelectField>
      </div>
      <ModelPicker provider={route.provider} model={route.model} onChange={setRoute} errors={{ model: tried ? modelErr : undefined }} />
      {isCliProvider(route.provider) && <CliSessionPicker provider={route.provider} value={cliSession} onChange={setCliSession} />}
      <FallbackChain value={fallbacks} onChange={setFallbacks} primary={route.provider} />
      {tried && fallbackErr && <p role="alert" className="text-sm text-danger -mt-3">{fallbackErr}</p>}
      <div className="grid gap-4 sm:grid-cols-[1fr_8rem] items-end">
        <div className="flex flex-col gap-1">
          <label htmlFor="seat-temp-range" className="text-sm font-medium">Temperature</label>
          <input id="seat-temp-range" type="range" min={0} max={2} step={0.1} value={temperature}
            aria-valuetext={`${temperature}`} className="w-full accent-[rgb(var(--accent))] h-9"
            onChange={(e) => { setTemperature(Number(e.target.value)); setTempValid(true); }} />
          <p className="text-xs text-fg-subtle">0 is focused and repeatable, 2 is loose and creative.</p>
        </div>
        <NumberField label="Exact value" min={0} max={2} step={0.1} value={temperature} className="self-start"
          onChange={(v) => v != null && setTemperature(v)} onValidityChange={setTempValid} />
      </div>
      <TextArea label="System prompt" value={prompt} maxLength={20000} rows={6} className="[&_textarea]:font-mono [&_textarea]:text-sm"
        hint={`${prompt.length.toLocaleString()}/20,000. The seat's standing instructions; topic and guardrails are appended at run time.`}
        onChange={(e) => setPrompt(e.target.value)} />
      <div className="flex flex-col gap-2">
        <TagInput label="Tools" value={tools} onChange={setTools} placeholder="web_search, fs_*"
          hint="Tools this seat may call (glob patterns allowed). Council guardrails still apply." />
        {!deepEqual([...tools].sort(), [...roleDefaults].sort()) && (
          <Button type="button" size="sm" variant="ghost" className="self-start" onClick={() => setTools(roleDefaults)}>
            Reset to {roleTitle(roles, role)} defaults
          </Button>
        )}
      </div>
      {error && <p role="alert" className="text-sm text-danger">{error}</p>}
      <div className="flex justify-end gap-2">
        <Button type="button" onClick={onCancel}>Cancel</Button>
        <Button type="submit" variant="primary" loading={pending}>{member ? "Save seat" : "Add seat"}</Button>
      </div>
    </form>
  );
}

// ------------------------------------------------------------------ Resize panel
export function ResizePanel({ current, target, min, max, preview, roles, overseers, newSeatRoute, onTarget, onApply, pending, error }: {
  current: number;
  target: number;
  min: number;
  max: number;
  preview: ResizePreview;
  roles?: RoleInfo[];
  overseers: Set<string>;
  newSeatRoute: string;
  onTarget: (n: number) => void;
  onApply: () => void;
  pending?: boolean;
  error?: string;
}) {
  const changed = target !== current;
  return (
    <div className="rounded-lg border border-border bg-surface-2 p-4 flex flex-col gap-3">
      <div className="flex flex-wrap items-end gap-4">
        <Stepper label="Council size" value={target} min={min} max={max} onChange={onTarget} error={error}
          hint={`Between ${min} and ${max} seats. Currently ${current}.`} />
        <div className="flex gap-2 pb-5">
          <Button size="sm" variant="primary" disabled={!changed} loading={pending} onClick={onApply}>
            {changed ? `Resize to ${target}` : "Resize"}
          </Button>
          {changed && <Button size="sm" variant="ghost" onClick={() => onTarget(current)}>Cancel</Button>}
        </div>
      </div>
      <div aria-live="polite">
      {changed && (
        <div className="flex flex-col gap-2">
          <p className="text-sm text-fg-muted">
            {preview.added.length > 0
              ? <>Adds {preview.added.length} seat{preview.added.length === 1 ? "" : "s"} from the mode's roster, on <span className="font-mono">{newSeatRoute}</span>:</>
              : <>Removes the last {preview.removed.length} seat{preview.removed.length === 1 ? "" : "s"}:</>}
          </p>
          <ul className="flex flex-wrap gap-1.5" aria-label="Resize preview">
            {preview.added.map((a, i) => (
              <RoleChip key={`a-${i}`} role={a.role} roles={roles} overseer={overseers.has(a.role)} tone="add">{a.name}</RoleChip>
            ))}
            {preview.removed.map((m) => (
              <RoleChip key={m.id} role={m.role} roles={roles} overseer={isOverseer(m)} tone="remove">{m.name}</RoleChip>
            ))}
          </ul>
        </div>
      )}
      </div>
    </div>
  );
}
