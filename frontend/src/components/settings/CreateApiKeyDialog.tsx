import { Check, Copy, KeyRound, TriangleAlert } from "lucide-react";
import { useId, useState, type FormEvent } from "react";

import { Button, Callout, Dialog, SelectField, TextField } from "@/components/ui";
import { cn } from "@/lib/cn";
import { errorMessage } from "@/lib/api";
import { useCreateApiKey, type ApiKeyCreated } from "@/features/settings/queries";
import { SCOPE_PRESETS, type ScopePreset } from "@/features/settings/scopes";

const EXPIRY = [
  { value: "30", label: "30 days" },
  { value: "90", label: "90 days" },
  { value: "365", label: "1 year" },
  { value: "", label: "Never" },
];

async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

function CopyButton({ text, label }: { text: string; label: string }) {
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");
  return (
    <Button type="button" size="sm" icon={state === "copied" ? Check : Copy} aria-label={label}
      onClick={async () => {
        const ok = await copyText(text);
        setState(ok ? "copied" : "failed");
        window.setTimeout(() => setState("idle"), 2000);
      }}>
      <span aria-live="polite">{state === "copied" ? "Copied" : state === "failed" ? "Select and copy" : "Copy"}</span>
    </Button>
  );
}

export function claudeCodeCommand(origin: string, key: string) {
  return `claude mcp add --transport http argus ${origin}/mcp --header 'Authorization: Bearer ${key}'`;
}

/** Step 2: the plaintext is shown once. Closing requires the explicit acknowledgement. */
function RevealKey({ created, onDone }: { created: ApiKeyCreated; onDone: () => void }) {
  const ackId = useId();
  const [ack, setAck] = useState(false);
  const cmd = claudeCodeCommand(window.location.origin, created.key);
  return (
    <div className="flex flex-col gap-4">
      <Callout tone="warning" title="Copy this key now">
        It won't be shown again. If you lose it, revoke it and create a new one.
      </Callout>
      <div className="flex flex-col gap-1">
        <div className="flex items-end gap-2">
          <TextField label={`Key for “${created.name}”`} value={created.key} readOnly spellCheck={false}
            className="flex-1 min-w-0 [&_input]:font-mono [&_input]:text-sm"
            onFocus={(e) => e.currentTarget.select()} />
          <CopyButton text={created.key} label="Copy API key" />
        </div>
      </div>
      <div className="flex flex-col gap-1.5">
        <p className="text-sm font-medium" id={`${ackId}-cc`}>Use with Claude Code</p>
        <div className="flex items-start gap-2">
          <pre role="region" aria-labelledby={`${ackId}-cc`} tabIndex={0}
            className="flex-1 min-w-0 overflow-x-auto rounded-md bg-surface-2 border border-border p-2.5 font-mono text-xs leading-5 whitespace-pre-wrap [overflow-wrap:anywhere]">
            {cmd}
          </pre>
          <CopyButton text={cmd} label="Copy Claude Code command" />
        </div>
        <p className="text-xs text-fg-subtle">Adds Argus as an MCP server so Claude Code can start and steer councils.</p>
      </div>
      <label htmlFor={ackId} className="flex items-start gap-2.5 rounded-md border border-border-strong p-3 cursor-pointer has-[:checked]:border-accent has-[:checked]:bg-accent-subtle">
        <input id={ackId} type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)}
          className="mt-0.5 size-4 accent-[rgb(var(--accent))]" />
        <span className="text-sm font-medium">
          I've stored it
          <span className="block text-xs font-normal text-fg-muted">The key is saved in a password manager or secret store.</span>
        </span>
      </label>
      <div className="flex justify-end">
        <Button variant="primary" disabled={!ack} onClick={onDone}>Done</Button>
      </div>
    </div>
  );
}

/** Step 1: name, access and expiry. */
function CreateForm({ onCreated, onCancel }: { onCreated: (k: ApiKeyCreated) => void; onCancel: () => void }) {
  const create = useCreateApiKey();
  const [name, setName] = useState("");
  const [preset, setPreset] = useState<ScopePreset>("runs");
  const [expiry, setExpiry] = useState("90");
  const [nameError, setNameError] = useState<string>();

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!name.trim()) {
      setNameError("Give the key a name so you can recognise it later.");
      return;
    }
    setNameError(undefined);
    const scopes = SCOPE_PRESETS.find((p) => p.value === preset)!.scopes;
    try {
      const k = await create.mutateAsync({ name: name.trim(), scopes, ...(expiry ? { expires_in_days: Number(expiry) } : {}) });
      onCreated(k);
    } catch {
      /* shown below */
    }
  };

  return (
    <form onSubmit={submit} noValidate className="flex flex-col gap-4">
      <TextField label="Name" value={name} maxLength={120} autoFocus placeholder="e.g. Laptop Claude Code, CI pipeline"
        error={nameError} onChange={(e) => setName(e.target.value)} />
      <fieldset className="flex flex-col gap-2">
        <legend className="text-sm font-medium mb-1">Access</legend>
        {SCOPE_PRESETS.map((p) => (
          <label key={p.value}
            className={cn("flex items-start gap-2.5 rounded-md border p-3 cursor-pointer transition-colors duration-fast",
              preset === p.value ? "border-accent bg-accent-subtle" : "border-border-strong hover:bg-surface-2")}>
            <input type="radio" name="scope-preset" value={p.value} checked={preset === p.value}
              onChange={() => setPreset(p.value)} className="mt-0.5 size-4 accent-[rgb(var(--accent))]" />
            <span className="text-sm font-medium">
              {p.label}
              <span className="block text-xs font-normal text-fg-muted">{p.description}</span>
            </span>
          </label>
        ))}
      </fieldset>
      <SelectField label="Expires" value={expiry} onChange={(e) => setExpiry(e.target.value)}
        hint={expiry ? undefined : "Keys that never expire are riskier if leaked."}
        className="sm:max-w-56">
        {EXPIRY.map((o) => <option key={o.label} value={o.value}>{o.label}</option>)}
      </SelectField>
      {create.error && <Callout tone="danger" title="Could not create the key">{errorMessage(create.error)}</Callout>}
      <div className="flex justify-end gap-2 mt-1">
        <Button type="button" variant="ghost" onClick={onCancel}>Cancel</Button>
        <Button type="submit" variant="primary" icon={KeyRound} loading={create.isPending}>Create key</Button>
      </div>
    </form>
  );
}

export function CreateApiKeyDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  // The plaintext lives only in this state and is dropped as soon as the user acknowledges.
  const [created, setCreated] = useState<ApiKeyCreated | null>(null);
  const [nudge, setNudge] = useState(false);

  const finish = () => {
    setCreated(null);
    setNudge(false);
    onOpenChange(false);
  };

  return (
    <Dialog open={open} wide={Boolean(created)}
      title={created ? "Your new API key" : "Create API key"}
      description={created ? undefined : "Keys let scripts, CI and Claude Code act as you through the API and MCP."}
      onOpenChange={(next) => {
        if (next) return onOpenChange(true);
        if (created) return setNudge(true); // never close while the plaintext is unacknowledged
        onOpenChange(false);
      }}>
      {created ? (
        <div className="flex flex-col gap-3">
          {nudge && (
            <p role="alert" className="flex items-center gap-2 text-sm text-danger">
              <TriangleAlert className="size-4 shrink-0" aria-hidden />
              Confirm you've stored the key before closing. It can't be shown again.
            </p>
          )}
          <RevealKey created={created} onDone={finish} />
        </div>
      ) : (
        // Radix unmounts closed content, so the form starts fresh every time it opens.
        <CreateForm onCreated={setCreated} onCancel={() => onOpenChange(false)} />
      )}
    </Dialog>
  );
}
