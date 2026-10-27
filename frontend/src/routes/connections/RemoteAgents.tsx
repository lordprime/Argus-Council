import { Bot, Globe, KeyRound, Network, Pencil, Plus, RefreshCw, Trash2 } from "lucide-react";
import { useState, type FormEvent } from "react";

import { ConnectionsPage } from "@/components/connections/ConnectionsNav";
import { ConfirmDelete } from "@/components/connections/ConfirmDelete";
import { CopyBlock } from "@/components/connections/CopyBlock";
import { CardSkeletons, Chips, Meta, SectionTitle, Switch } from "@/components/connections/parts";
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
  Segmented,
  TextArea,
  TextField,
  Toggle,
} from "@/components/ui";
import {
  summarizeCard,
  useCreateAgent,
  useDeleteAgent,
  useRefreshAgent,
  useUpdateAgent,
  type AgentPatch,
  type RefreshResult,
  type RemoteAgent,
} from "@/features/connections/queries";
import { errorMessage } from "@/lib/api";
import { useRemoteAgents } from "@/lib/queries";
import { toast } from "@/stores/toast";

const NAME_RE = /^[a-z0-9][a-z0-9_-]*$/;
type Protocol = "a2a" | "acp";

const str = (v: unknown): string => (typeof v === "string" ? v : typeof v === "number" ? String(v) : "");

// ------------------------------------------------------------------ dialog
function AgentDialog({ open, onOpenChange, existing, takenNames }: {
  open: boolean;
  onOpenChange: (o: boolean) => void;
  existing?: RemoteAgent;
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
  const [requiresApproval, setRequiresApproval] = useState(existing?.requires_approval ?? true);
  const [token, setToken] = useState<SecretAction>(existing ? { kind: "keep" } : { kind: "replace", value: "" });
  const [touched, setTouched] = useState(false);

  const errors: Record<string, string> = {};
  if (!existing) {
    if (!NAME_RE.test(name)) errors.name = "Lowercase letters, digits, '-' or '_' (max 40)";
    else if (takenNames.includes(name)) errors.name = `An agent named ${name} already exists`;
  }
  if (protocol === "acp" && !agentName.trim()) errors.agentName = "ACP needs the agent's name on that server";
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
        const patch: AgentPatch = { url: url.trim(), description, enabled, requires_approval: requiresApproval, ...(t !== undefined ? { token: t } : {}) };
        if (existing.protocol === "acp" && agentName.trim() !== (existing.agent_name ?? "")) patch.agent_name = agentName.trim();
        await update.mutateAsync({ id: existing.id, patch });
        toast({ tone: "success", title: `Saved ${existing.name}` });
      } else {
        await create.mutateAsync({
          name, protocol, url: url.trim(), description, enabled, requires_approval: requiresApproval,
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
        {protocol === "acp" && (
          <TextField label="Agent name" value={agentName} onChange={(e) => setAgentName(e.target.value)} spellCheck={false} maxLength={120}
            hint={existing ? "The agent's name on the ACP server. Changing it clears the cached card; refresh afterwards." : "The agent's name on the ACP server"}
            error={show("agentName")} />
        )}
        <SecretField label="Bearer token (optional)" stored={Boolean(existing?.has_token)} action={token} onChange={setToken} />
        <TextArea label="Description" value={description} onChange={(e) => setDescription(e.target.value)} maxLength={2000} rows={3}
          hint="Shown to the council chair when it decides whom to delegate to. Filled from the agent card if left blank." />
        <Toggle label="Require approval before delegating" checked={requiresApproval} onChange={setRequiresApproval}
          hint={requiresApproval
            ? "Each time a council hands work to this agent, the run pauses until you approve it."
            : "Councils can delegate to this agent without asking. Only turn this off for agents you control."} />
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
  const summary = summarizeCard(card);
  if (!summary) return <p className="text-sm text-fg-subtle">No card fetched yet. Use Refresh card.</p>;
  const { skills, capabilities, inputModes, outputModes } = summary;
  const schemes = card.securitySchemes && typeof card.securitySchemes === "object" ? Object.keys(card.securitySchemes) : [];
  return (
    <div className="flex flex-col gap-2 rounded-md bg-surface-2 p-3">
      <div className="flex flex-wrap items-baseline gap-x-2">
        <span className="font-medium">{str(card.name) || "Unnamed agent"}</span>
        {str(card.version) && <span className="text-xs text-fg-subtle font-mono">v{str(card.version)}</span>}
      </div>
      {str(card.description) && <p className="text-sm text-fg-muted">{str(card.description)}</p>}
      <dl className="grid gap-2 sm:grid-cols-2">
        <Meta label="Skills">{skills.length ? `${skills.length} ${skills.length === 1 ? "skill" : "skills"}` : "None declared"}</Meta>
        <Meta label="Capabilities"><Chips items={capabilities} label={`${label} capabilities`} empty="None declared" /></Meta>
        {(inputModes.length > 0 || outputModes.length > 0) && (
          <Meta label="Content types" mono>{[...new Set([...inputModes, ...outputModes])].join(", ")}</Meta>
        )}
        {str(card.url) && <Meta label="Endpoint" mono>{str(card.url)}</Meta>}
        <Meta label="Auth">{schemes.length ? schemes.join(", ") : "None declared"}</Meta>
      </dl>
      {skills.length > 0 && (
        <div>
          <p className="text-xs text-fg-subtle mb-1">Skills</p>
          <ul aria-label={`${label} skills`} className="flex flex-col gap-1">
            {skills.slice(0, 12).map((s) => (
              <li key={s.id} className="text-sm break-words">
                <span className="font-medium">{s.name}</span>
                {s.description && <span className="text-fg-muted"> · {s.description}</span>}
              </li>
            ))}
          </ul>
          {skills.length > 12 && <p className="text-xs text-fg-subtle mt-1">and {skills.length - 12} more</p>}
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

function RefreshSummary({ r }: { r: RefreshResult }) {
  if (!r.ok) return <Callout tone="danger" title={`Could not reach the agent · ${r.latencyMs} ms`}>{r.error}</Callout>;
  const sum = summarizeCard(r.agent?.card);
  const n = sum?.skills.length ?? 0;
  return (
    <Callout tone="success" title={`Refreshed · ${r.latencyMs} ms`}>
      {r.agent?.protocol === "acp" ? "The ACP server lists this agent." : "Fetched the agent card."}
      {sum && ` ${n} ${n === 1 ? "skill" : "skills"}${sum.capabilities.length ? `, capabilities: ${sum.capabilities.join(", ")}` : ""}.`}
    </Callout>
  );
}

const PROTOCOL: Record<Protocol, { label: string; tone: "info" | "accent"; title: string }> = {
  a2a: { label: "A2A", tone: "info", title: "Agent2Agent (JSON-RPC with an agent card)" },
  acp: { label: "ACP", tone: "accent", title: "Agent Communication Protocol (REST)" },
};

function AgentCard({ a, onEdit, onDelete }: { a: RemoteAgent; onEdit: () => void; onDelete: () => void }) {
  const refresh = useRefreshAgent();
  const update = useUpdateAgent();
  const headingId = `agent-${a.id}`;
  const needsApproval = a.requires_approval ?? true;
  const toggle = (enabled: boolean) =>
    update.mutate({ id: a.id, patch: { enabled } }, {
      onError: (e) => toast({ tone: "danger", title: `Could not update ${a.name}`, body: errorMessage(e) }),
    });
  return (
    <Card as="article" aria-labelledby={headingId} className="p-4 flex flex-col gap-3">
      <div className="flex flex-wrap items-start gap-3">
        <span className="grid place-items-center size-9 rounded-md border border-dashed border-border-strong text-fg-muted shrink-0">
          <Globe className="size-5" aria-hidden />
        </span>
        <div className="min-w-0 flex-1 basis-40">
          <h3 id={headingId} className="font-semibold truncate">{a.name}</h3>
          <p className="text-xs text-fg-subtle font-mono truncate">{a.url}</p>
        </div>
        <div className="flex flex-wrap gap-1">
          <span title={PROTOCOL[a.protocol].title}>
            <Badge tone={PROTOCOL[a.protocol].tone}><span className="sr-only">Protocol: </span>{PROTOCOL[a.protocol].label}</Badge>
          </span>
          {needsApproval ? <Badge tone="warning">Needs approval</Badge> : <Badge tone="neutral">No approval</Badge>}
          {!a.enabled && <Badge>Disabled</Badge>}
        </div>
      </div>
      {a.description && <p className="text-sm text-fg-muted">{a.description}</p>}
      <dl className="grid gap-3 sm:grid-cols-2">
        {a.protocol === "acp" && <Meta label="Agent name" mono>{a.agent_name ?? "—"}</Meta>}
        <Meta label="Delegation">{needsApproval ? "Each delegation needs your approval" : "Runs without asking"}</Meta>
        <Meta label="Token">
          {a.has_token ? <span className="inline-flex items-center gap-1"><KeyRound className="size-3.5" aria-hidden />Stored · write-only</span> : "None"}
        </Meta>
      </dl>
      {refresh.data && <RefreshSummary r={refresh.data} />}
      <AgentCardView card={a.card ?? {}} label={a.name} />
      <div className="flex flex-wrap items-center gap-2 pt-3 border-t border-border">
        <Switch object={a.name} checked={a.enabled} onChange={toggle} disabled={update.isPending} />
        <div className="flex-1" />
        <Button size="sm" icon={RefreshCw} loading={refresh.isPending}
          aria-label={`Refresh card for ${a.name}`} onClick={() => refresh.mutate(a.id)}>
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
  const [dialog, setDialog] = useState<{ edit?: RemoteAgent } | null>(null);
  const [deleting, setDeleting] = useState<RemoteAgent | null>(null);
  const agents: RemoteAgent[] = q.data ?? [];
  const origin = window.location.origin;

  return (
    <ConnectionsPage title="Remote agents" description="Seat agents from other systems in your councils over A2A or ACP, and expose Argus to them."
      actions={<Button variant="primary" icon={Plus} onClick={() => setDialog({})} disabled={q.isLoading}>Add agent</Button>}>

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
              <EmptyState titleAs="h3" icon={Bot} title="No remote agents yet"
                body="Paste an A2A endpoint to read its agent card, or add an ACP server and agent name." />
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

