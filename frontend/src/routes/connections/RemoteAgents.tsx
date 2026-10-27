import { Bot, Globe, KeyRound, Network, Pencil, Plus, RefreshCw, Trash2 } from "lucide-react";
import { useState, type FormEvent } from "react";

import { ConnectionsPage } from "@/components/connections/ConnectionsNav";
import { ConfirmDelete } from "@/components/connections/ConfirmDelete";
import { CopyBlock } from "@/components/connections/CopyBlock";
import { CardSkeletons, Meta, SectionTitle, Switch } from "@/components/connections/parts";
import { SecretField, secretPatch, type SecretAction } from "@/components/connections/SecretField";
import {
  Badge,
  Button,
  Callout,
  Card,
  Dialog,
  EmptyState,
  ErrorState,
  IconButton,
  PageHeader,
  Segmented,
  TextArea,
  TextField,
  Toggle,
} from "@/components/ui";
import { useCreateAgent, useDeleteAgent, useRefreshAgent, useUpdateAgent, type AgentPatch } from "@/features/connections/queries";
import { errorMessage } from "@/lib/api";
import { useRemoteAgents } from "@/lib/queries";
import type { RemoteAgentInfo } from "@/lib/types";
import { toast } from "@/stores/toast";

const NAME_RE = /^[a-z0-9][a-z0-9_-]*$/;
type Protocol = "a2a" | "acp";

const str = (v: unknown): string => (typeof v === "string" ? v : typeof v === "number" ? String(v) : "");

// ------------------------------------------------------------------ dialog
function AgentDialog({ open, onOpenChange, existing, takenNames }: {
  open: boolean;
  onOpenChange: (o: boolean) => void;
  existing?: RemoteAgentInfo;
  takenNames: string[];
}) {
  const create = useCreateAgent();
  const update = useUpdateAgent();
  const mutation = existing ? update : create;
  const [protocol, setProtocol] = useState<Protocol>(existing?.protocol ?? "a2a");
  const [name, setName] = useState(existing?.name ?? "");
  const [url, setUrl] = useState(existing?.url ?? "");
  const [agentName, setAgentName] = useState(existing?.agent_name ?? "");
  const [description, setDescription] = useState(existing?.description ?? "");
  const [enabled, setEnabled] = useState(existing?.enabled ?? true);
  const [token, setToken] = useState<SecretAction>(existing ? { kind: "keep" } : { kind: "replace", value: "" });
  const [touched, setTouched] = useState(false);

  const errors: Record<string, string> = {};
  if (!existing) {
    if (!NAME_RE.test(name)) errors.name = "Lowercase letters, digits, '-' or '_' (max 40)";
    else if (takenNames.includes(name)) errors.name = `An agent named ${name} already exists`;
    if (protocol === "acp" && !agentName.trim()) errors.agentName = "ACP needs the agent's name on that server";
  }
  if (!/^https?:\/\/\S+$/i.test(url.trim())) errors.url = "Enter an http(s) URL";
  const invalid = Object.keys(errors).length > 0;
  const show = (k: string) => (touched ? errors[k] : undefined);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setTouched(true);
    if (invalid) return;
    try {
      if (existing) {
        const t = secretPatch(token);
        const patch: AgentPatch = { url: url.trim(), description, enabled, ...(t !== undefined ? { token: t } : {}) };
        await update.mutateAsync({ id: existing.id, patch });
        toast({ tone: "success", title: `Saved ${existing.name}` });
      } else {
        await create.mutateAsync({
          name, protocol, url: url.trim(), description, enabled,
          agent_name: protocol === "acp" ? agentName.trim() : null,
          token: secretPatch(token) ?? null,
        });
        toast({ tone: "success", title: `Added ${name}`, body: "Use Refresh card to check the connection." });
      }
      onOpenChange(false);
    } catch {
      /* shown below */
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange} title={existing ? `Edit ${existing.name}` : "Add a remote agent"}
      description="Remote agents can be seated in a council, shown with an external badge.">
      <form onSubmit={submit} noValidate className="flex flex-col gap-4">
        {touched && invalid && <Callout tone="danger" title="Fix the highlighted fields">{Object.values(errors).join(" · ")}</Callout>}
        {!existing && (
          <Segmented label="Protocol" value={protocol} onChange={setProtocol} options={[
            { value: "a2a", label: "A2A", hint: "Agent2Agent JSON-RPC with an agent card", icon: Globe },
            { value: "acp", label: "ACP", hint: "Agent Communication Protocol REST server", icon: Network },
          ]} />
        )}
        {!existing && (
          <TextField label="Name" value={name} onChange={(e) => setName(e.target.value)} maxLength={40} spellCheck={false}
            hint="Short id used in councils, e.g. partner-research" error={show("name")} />
        )}
        <TextField label={protocol === "a2a" ? "A2A endpoint URL" : "ACP base URL"} type="url" inputMode="url" value={url}
          onChange={(e) => setUrl(e.target.value)} spellCheck={false} error={show("url")}
          placeholder={protocol === "a2a" ? "https://agent.example.com/a2a" : "https://agents.example.com/acp"}
          hint={protocol === "a2a" ? "The JSON-RPC endpoint. The agent card is read from /.well-known/agent-card.json on the same host." : "Argus calls /agents and /runs under this URL."} />
        {protocol === "acp" && !existing && (
          <TextField label="Agent name" value={agentName} onChange={(e) => setAgentName(e.target.value)} spellCheck={false}
            hint="The agent's name on the ACP server" error={show("agentName")} />
        )}
        <SecretField label="Bearer token (optional)" stored={Boolean(existing?.has_token)} action={token} onChange={setToken} />
        <TextArea label="Description" value={description} onChange={(e) => setDescription(e.target.value)} maxLength={2000} rows={3}
          hint="Shown to the council chair when it decides whom to delegate to. Filled from the agent card if left blank." />
        <Toggle label="Enabled" checked={enabled} onChange={setEnabled} />
        {mutation.error && <Callout tone="danger" title="Could not save">{errorMessage(mutation.error)}</Callout>}
        <div className="flex justify-end gap-2">
          <Button type="button" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button type="submit" variant="primary" loading={mutation.isPending}>{existing ? "Save" : "Add agent"}</Button>
        </div>
      </form>
    </Dialog>
  );
}

// ------------------------------------------------------------------ card
/** Renders a fetched agent card. Remote data is untrusted: plain text only, never HTML or Markdown. */
function AgentCardView({ card, label }: { card: Record<string, unknown>; label: string }) {
  if (!Object.keys(card).length) return <p className="text-sm text-fg-subtle">No card fetched yet. Use Refresh card.</p>;
  const skills = Array.isArray(card.skills) ? (card.skills as Record<string, unknown>[]) : [];
  const schemes = card.securitySchemes && typeof card.securitySchemes === "object" ? Object.keys(card.securitySchemes) : [];
  return (
    <div className="flex flex-col gap-2 rounded-md bg-surface-2 p-3">
      <div className="flex flex-wrap items-baseline gap-x-2">
        <span className="font-medium">{str(card.name) || "Unnamed agent"}</span>
        {str(card.version) && <span className="text-xs text-fg-subtle font-mono">v{str(card.version)}</span>}
      </div>
      {str(card.description) && <p className="text-sm text-fg-muted">{str(card.description)}</p>}
      <dl className="grid gap-2 sm:grid-cols-2">
        {str(card.url) && <Meta label="Endpoint" mono>{str(card.url)}</Meta>}
        <Meta label="Auth">{schemes.length ? schemes.join(", ") : "None declared"}</Meta>
      </dl>
      {skills.length > 0 && (
        <div>
          <p className="text-xs text-fg-subtle mb-1">Skills</p>
          <ul aria-label={`${label} skills`} className="flex flex-col gap-1">
            {skills.slice(0, 12).map((s, i) => (
              <li key={str(s.id) || i} className="text-sm">
                <span className="font-medium">{str(s.name) || str(s.id)}</span>
                {str(s.description) && <span className="text-fg-muted"> · {str(s.description)}</span>}
              </li>
            ))}
          </ul>
        </div>
      )}
      <details className="text-xs">
        <summary className="cursor-pointer text-fg-muted min-h-6 inline-flex items-center">Raw card JSON</summary>
        <pre tabIndex={0} role="region" aria-label={`${label} raw card`} className="mt-1 max-h-60 overflow-auto font-mono whitespace-pre-wrap break-all">
          {JSON.stringify(card, null, 2)}
        </pre>
      </details>
    </div>
  );
}

function AgentCard({ a, onEdit, onDelete }: { a: RemoteAgentInfo; onEdit: () => void; onDelete: () => void }) {
  const refresh = useRefreshAgent();
  const update = useUpdateAgent();
  const headingId = `agent-${a.id}`;
  const toggle = (enabled: boolean) =>
    update.mutate({ id: a.id, patch: { enabled } }, {
      onError: (e) => toast({ tone: "danger", title: `Could not update ${a.name}`, body: errorMessage(e) }),
    });
  return (
    <Card as="article" aria-labelledby={headingId} className="p-4 flex flex-col gap-3">
      <div className="flex items-start gap-3">
        <span className="grid place-items-center size-9 rounded-md border border-dashed border-border-strong text-fg-muted shrink-0">
          <Globe className="size-5" aria-hidden />
        </span>
        <div className="min-w-0 flex-1">
          <h3 id={headingId} className="font-semibold truncate">{a.name}</h3>
          <p className="text-xs text-fg-subtle font-mono truncate">{a.url}</p>
        </div>
        <Badge tone="accent">{a.protocol.toUpperCase()}</Badge>
        {!a.enabled && <Badge>Disabled</Badge>}
      </div>
      {a.description && <p className="text-sm text-fg-muted">{a.description}</p>}
      <dl className="grid gap-3 sm:grid-cols-2">
        {a.protocol === "acp" && <Meta label="Agent name" mono>{a.agent_name ?? "—"}</Meta>}
        <Meta label="Token">
          {a.has_token ? <span className="inline-flex items-center gap-1"><KeyRound className="size-3.5" aria-hidden />Stored · write-only</span> : "None"}
        </Meta>
      </dl>
      {refresh.error && <Callout tone="danger" title="Could not reach the agent">{errorMessage(refresh.error)}</Callout>}
      <AgentCardView card={a.card ?? {}} label={a.name} />
      <div className="flex flex-wrap items-center gap-2 pt-3 border-t border-border">
        <Switch object={a.name} checked={a.enabled} onChange={toggle} disabled={update.isPending} />
        <div className="flex-1" />
        <Button size="sm" icon={RefreshCw} loading={refresh.isPending}
          onClick={() => refresh.mutate(a.id, { onSuccess: () => toast({ tone: "success", title: `Refreshed ${a.name}` }) })}>
          Refresh card
        </Button>
        <IconButton label={`Edit ${a.name}`} icon={Pencil} onClick={onEdit} />
        <IconButton label={`Delete ${a.name}`} icon={Trash2} variant="danger-ghost" onClick={onDelete} />
      </div>
    </Card>
  );
}

// ------------------------------------------------------------------ page
export default function RemoteAgents() {
  const q = useRemoteAgents();
  const del = useDeleteAgent();
  const [dialog, setDialog] = useState<{ edit?: RemoteAgentInfo } | null>(null);
  const [deleting, setDeleting] = useState<RemoteAgentInfo | null>(null);
  const agents = q.data ?? [];
  const origin = window.location.origin;

  return (
    <ConnectionsPage>
      <PageHeader title="Remote agents" description="Seat agents from other systems in your councils over A2A or ACP, and expose Argus to them."
        actions={<Button variant="primary" icon={Plus} onClick={() => setDialog({})} disabled={q.isLoading}>Add agent</Button>} />

      <div className="flex flex-col gap-8">
        <section aria-labelledby="agents-h">
          <SectionTitle id="agents-h">Connected agents</SectionTitle>
          {q.isLoading ? (
            <CardSkeletons count={2} />
          ) : q.error ? (
            <Card><ErrorState error={q.error} onRetry={() => void q.refetch()} /></Card>
          ) : agents.length ? (
            <div className="grid gap-4 lg:grid-cols-2 items-start">
              {agents.map((a) => <AgentCard key={a.id} a={a} onEdit={() => setDialog({ edit: a })} onDelete={() => { del.reset(); setDeleting(a); }} />)}
            </div>
          ) : (
            <Card as="div">
              <EmptyState icon={Bot} title="No remote agents yet"
                body="Paste an A2A endpoint to read its agent card, or add an ACP server and agent name."
                action={<Button variant="primary" icon={Plus} onClick={() => setDialog({})}>Add agent</Button>} />
            </Card>
          )}
        </section>

        <section aria-labelledby="self-h">
          <SectionTitle id="self-h" description="Other agent systems can call Argus councils with a bearer API key.">Argus as a remote agent</SectionTitle>
          <Card className="p-4 grid gap-4 md:grid-cols-2">
            <div className="flex flex-col gap-1.5 min-w-0">
              <p className="text-sm font-medium">A2A agent card</p>
              <CopyBlock label="A2A agent card URL" value={`${origin}/.well-known/agent-card.json`} />
              <p className="text-xs text-fg-subtle">JSON-RPC endpoint: <span className="font-mono">{origin}/a2a</span></p>
            </div>
            <div className="flex flex-col gap-1.5 min-w-0">
              <p className="text-sm font-medium">ACP base URL</p>
              <CopyBlock label="ACP base URL" value={`${origin}/acp`} />
              <p className="text-xs text-fg-subtle">Agents: argus-research, argus-coding, argus-chat, argus-general, plus council-&lt;name&gt; for each saved council.</p>
            </div>
          </Card>
        </section>
      </div>

      {dialog && (
        <AgentDialog key={dialog.edit?.id ?? "new"} open onOpenChange={(o) => !o && setDialog(null)} existing={dialog.edit}
          takenNames={agents.map((a) => a.name)} />
      )}
      {deleting && (
        <ConfirmDelete open onOpenChange={(o) => !o && setDeleting(null)} kind="remote agent" name={deleting.name}
          consequence={<>Council seats using <strong>{deleting.name}</strong> stop working. Its stored token is destroyed.</>}
          pending={del.isPending} error={del.error}
          onConfirm={() => del.mutate(deleting.id, { onSuccess: () => { toast({ tone: "success", title: `Deleted ${deleting.name}` }); setDeleting(null); } })} />
      )}
    </ConnectionsPage>
  );
}

