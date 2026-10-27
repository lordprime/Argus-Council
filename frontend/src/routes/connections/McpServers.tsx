import { Globe, KeyRound, Pencil, Plus, RefreshCw, Server, ShieldAlert, Terminal, Trash2, Wrench } from "lucide-react";
import { useState, type FormEvent } from "react";
import { Link } from "react-router";

import { ConnectionsPage } from "@/components/connections/ConnectionsNav";
import { ConfirmDelete } from "@/components/connections/ConfirmDelete";
import { CopyBlock } from "@/components/connections/CopyBlock";
import { CardSkeletons, Chips, Meta, rowsToDict, SecretRows, SectionTitle, splitList, type KV, Switch } from "@/components/connections/parts";
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
import { useCreateMcp, useDeleteMcp, useDiscoverMcp, useUpdateMcp, type McpPatch } from "@/features/connections/queries";
import { errorMessage } from "@/lib/api";
import { useMcpServers } from "@/lib/queries";
import type { McpServerInfo } from "@/lib/types";
import { useSession } from "@/stores/session";
import { toast } from "@/stores/toast";

const NAME_RE = /^[a-z0-9][a-z0-9_-]*$/;
const RISKS = [
  { key: "network", label: "Network reads" },
  { key: "network_write", label: "Network writes" },
  { key: "fs_write", label: "File writes" },
  { key: "shell", label: "Shell" },
  { key: "package_install", label: "Package installs" },
  { key: "remote_agent", label: "Remote agents" },
];

/** fnmatch-style match for tool_allow patterns ("*", "search_*"). */
function toolAllowed(tool: string, patterns: string[]): boolean {
  return patterns.some((p) => new RegExp(`^${p.replace(/[.+^${}()|[\]\\]/g, "\\$&").replace(/\*/g, ".*").replace(/\?/g, ".")}$`).test(tool));
}

function origin() {
  return typeof window === "undefined" ? "https://argus.example" : window.location.origin;
}

// ------------------------------------------------------------------ dialog
function McpDialog({ open, onOpenChange, existing, stdioAllowed, takenNames }: {
  open: boolean;
  onOpenChange: (o: boolean) => void;
  existing?: McpServerInfo;
  stdioAllowed: boolean;
  takenNames: string[];
}) {
  const create = useCreateMcp();
  const update = useUpdateMcp();
  const mutation = existing ? update : create;
  const [name, setName] = useState(existing?.name ?? "");
  const [transport, setTransport] = useState<"http" | "stdio">(existing?.transport ?? "http");
  const [url, setUrl] = useState(existing?.url ?? "");
  const [command, setCommand] = useState(existing?.command.join("\n") ?? "");
  const [headers, setHeaders] = useState<KV[]>(existing ? [] : [{ key: "Authorization", value: "" }]);
  const [replaceHeaders, setReplaceHeaders] = useState(!existing);
  const [clearHeaders, setClearHeaders] = useState(false);
  const [toolAllow, setToolAllow] = useState((existing?.tool_allow ?? ["*"]).join(", "));
  const [risk, setRisk] = useState<string[]>(existing?.risk ?? ["network"]);
  const [enabled, setEnabled] = useState(existing?.enabled ?? true);
  const [touched, setTouched] = useState(false);

  const errors: Record<string, string> = {};
  if (!existing) {
    if (!NAME_RE.test(name)) errors.name = "Lowercase letters, digits, '-' or '_' (max 40)";
    else if (takenNames.includes(name)) errors.name = `A server named ${name} already exists`;
  }
  if (transport === "http" && !/^https?:\/\/\S+$/i.test(url.trim())) errors.url = "Enter the server's http(s) URL";
  if (transport === "stdio" && !command.trim()) errors.command = "Enter the command to run";
  const invalid = Object.keys(errors).length > 0;
  const show = (k: string) => (touched ? errors[k] : undefined);
  const secretsLabel = transport === "stdio" ? "Environment variables" : "Headers";

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setTouched(true);
    if (invalid) return;
    const tool_allow = splitList(toolAllow);
    try {
      if (existing) {
        const patch: McpPatch = { tool_allow: tool_allow.length ? tool_allow : ["*"], risk, enabled };
        if (existing.transport === "http") patch.url = url.trim();
        if (clearHeaders) patch.headers = {};
        else if (replaceHeaders && Object.keys(rowsToDict(headers)).length) patch.headers = rowsToDict(headers);
        await update.mutateAsync({ id: existing.id, patch });
        toast({ tone: "success", title: `Saved ${existing.name}` });
      } else {
        await create.mutateAsync({
          name, transport, enabled, risk,
          tool_allow: tool_allow.length ? tool_allow : ["*"],
          ...(transport === "http" ? { url: url.trim() } : { command: command.split("\n").map((s) => s.trim()).filter(Boolean) }),
          headers: rowsToDict(headers),
        });
        toast({ tone: "success", title: `Added ${name}`, body: "Discover its tools to check the connection." });
      }
      onOpenChange(false);
    } catch {
      /* shown below */
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange} wide title={existing ? `Edit ${existing.name}` : "Add an MCP server"}
      description="Tools from this server become available to council members, subject to guardrails.">
      <form onSubmit={submit} noValidate className="flex flex-col gap-4">
        {touched && invalid && <Callout tone="danger" title="Fix the highlighted fields">{Object.values(errors).join(" · ")}</Callout>}
        {!existing && (
          <div className="grid gap-4 sm:grid-cols-2">
            <TextField label="Name" value={name} onChange={(e) => setName(e.target.value)} maxLength={40} spellCheck={false}
              hint="Tools appear as name.tool" error={show("name")} />
            <div>
              <Segmented label="Transport" value={transport} onChange={setTransport} options={[
                { value: "http", label: "Streamable HTTP", icon: Globe },
                ...(stdioAllowed ? [{ value: "stdio" as const, label: "stdio", icon: Terminal }] : []),
              ]} />
              {!stdioAllowed && <p className="text-xs text-fg-subtle mt-1">stdio servers are disabled on this deployment or need an administrator.</p>}
            </div>
          </div>
        )}
        {transport === "http" ? (
          <TextField label="Server URL" type="url" inputMode="url" value={url} onChange={(e) => setUrl(e.target.value)}
            placeholder="https://mcp.example.com/mcp" error={show("url")} spellCheck={false} />
        ) : (
          <TextArea label="Command" value={command} onChange={(e) => setCommand(e.target.value)} disabled={Boolean(existing)}
            placeholder={"npx\n-y\n@modelcontextprotocol/server-filesystem\n/data"} className="font-mono" rows={4}
            hint={existing ? "The command cannot be changed. Delete and re-add the server instead." : "One argument per line."}
            error={show("command")} spellCheck={false} />
        )}

        {existing && !replaceHeaders ? (
          <div className="flex flex-col gap-1">
            <span className="text-sm font-medium">{secretsLabel}</span>
            <div className="flex flex-wrap items-center gap-2">
              {existing.header_names.length ? (
                <ul aria-label={`Stored ${secretsLabel.toLowerCase()}`} className="flex flex-wrap gap-1">
                  {existing.header_names.map((h) => (
                    <li key={h} className={`inline-flex items-center gap-1 h-7 rounded-sm border border-border bg-surface-2 px-2 font-mono text-xs ${clearHeaders ? "line-through text-fg-subtle" : "text-fg-muted"}`}>
                      <KeyRound className="size-3" aria-hidden />{h}: ••••••
                    </li>
                  ))}
                </ul>
              ) : <span className="text-sm text-fg-subtle">None stored</span>}
              <Button type="button" size="sm" onClick={() => { setReplaceHeaders(true); setClearHeaders(false); setHeaders([{ key: "", value: "" }]); }}>
                Replace all
              </Button>
              {existing.header_names.length > 0 && (
                clearHeaders
                  ? <Button type="button" size="sm" variant="ghost" onClick={() => setClearHeaders(false)}>Undo remove</Button>
                  : <Button type="button" size="sm" variant="danger-ghost" onClick={() => setClearHeaders(true)}>Remove all</Button>
              )}
            </div>
            <p className="text-xs text-fg-subtle">Values are write-only: only their names are shown.</p>
          </div>
        ) : (
          <SecretRows legend={secretsLabel} rows={headers} onChange={setHeaders}
            namePlaceholder={transport === "stdio" ? "API_TOKEN" : "Authorization"}
            hint={existing ? "These replace every stored value. Values are encrypted and never shown again." : "Values are encrypted and never shown again."} />
        )}

        <TextField label="Allowed tools" value={toolAllow} onChange={(e) => setToolAllow(e.target.value)} spellCheck={false}
          hint="Comma-separated names or patterns. * allows every tool the server offers." />
        <fieldset>
          <legend className="text-sm font-medium mb-1">Risk tags</legend>
          <p className="text-xs text-fg-subtle mb-2">Guardrails use these to decide when calls need your approval.</p>
          <div className="flex flex-wrap gap-2">
            {RISKS.map((r) => {
              const on = risk.includes(r.key);
              return (
                <button key={r.key} type="button" aria-pressed={on}
                  onClick={() => setRisk((x) => (on ? x.filter((k) => k !== r.key) : [...x, r.key]))}
                  className={`h-8 px-3 rounded-full border text-sm ${on ? "bg-accent-subtle border-accent text-accent font-medium" : "border-border-strong text-fg-muted"}`}>
                  {r.label}
                </button>
              );
            })}
          </div>
        </fieldset>
        <Toggle label="Enabled" checked={enabled} onChange={setEnabled} />
        {mutation.error && <Callout tone="danger" title="Could not save">{errorMessage(mutation.error)}</Callout>}
        <div className="flex justify-end gap-2">
          <Button type="button" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button type="submit" variant="primary" loading={mutation.isPending}>{existing ? "Save" : "Add server"}</Button>
        </div>
      </form>
    </Dialog>
  );
}

// ------------------------------------------------------------------ server card
function ServerCard({ s, onEdit, onDelete }: { s: McpServerInfo; onEdit: () => void; onDelete: () => void }) {
  const discover = useDiscoverMcp();
  const update = useUpdateMcp();
  const headingId = `mcp-${s.id}`;
  const toggle = (enabled: boolean) =>
    update.mutate({ id: s.id, patch: { enabled } }, {
      onError: (e) => toast({ tone: "danger", title: `Could not update ${s.name}`, body: errorMessage(e) }),
    });
  const run = () =>
    discover.mutate(s.id, {
      onSuccess: (r) => toast({ tone: "success", title: `${s.name}: ${r.tools.length} tools`, body: r.server_info?.name ? `${r.server_info.name} ${r.server_info.version ?? ""}` : undefined }),
    });

  return (
    <Card as="article" aria-labelledby={headingId} className="p-4 flex flex-col gap-3">
      <div className="flex items-start gap-3">
        <span className="grid place-items-center size-9 rounded-md bg-accent-subtle text-accent shrink-0">
          {s.transport === "stdio" ? <Terminal className="size-5" aria-hidden /> : <Server className="size-5" aria-hidden />}
        </span>
        <div className="min-w-0 flex-1">
          <h3 id={headingId} className="font-semibold truncate">{s.name}</h3>
          <p className="text-xs text-fg-subtle font-mono truncate">{s.transport === "http" ? s.url : s.command.join(" ")}</p>
        </div>
        <Badge tone={s.transport === "stdio" ? "warning" : "info"}>{s.transport === "stdio" ? "stdio" : "HTTP"}</Badge>
        {!s.enabled && <Badge>Disabled</Badge>}
      </div>
      <dl className="grid gap-3 sm:grid-cols-3">
        <Meta label={s.transport === "stdio" ? "Environment" : "Headers"}>
          {s.header_names.length ? (
            <span className="inline-flex flex-wrap items-center gap-1 font-mono text-xs">
              <KeyRound className="size-3" aria-hidden />{s.header_names.join(", ")}
              <span className="sr-only">(values are write-only)</span>
            </span>
          ) : "None"}
        </Meta>
        <Meta label="Allowed tools" mono>{s.tool_allow.join(", ") || "None"}</Meta>
        <Meta label="Risk tags"><Chips items={s.risk} label={`Risk tags for ${s.name}`} /></Meta>
      </dl>
      {discover.error && (
        <Callout tone="danger" title="Could not reach the server">{errorMessage(discover.error)}</Callout>
      )}
      <div>
        <h4 className="text-sm font-medium mb-1.5">Discovered tools <span className="text-fg-subtle font-normal">({s.tools.length})</span></h4>
        {s.tools.length ? (
          <div tabIndex={0} role="region" aria-label={`Tools from ${s.name}`} className="relative max-h-60 overflow-auto rounded-md border border-border">
            <table className="w-full text-sm">
              <thead className="bg-surface-2 text-left text-xs text-fg-subtle sticky top-0">
                <tr><th scope="col" className="px-3 py-1.5 font-medium">Tool</th><th scope="col" className="px-3 py-1.5 font-medium">Description</th><th scope="col" className="px-3 py-1.5 font-medium">Allowed</th></tr>
              </thead>
              <tbody className="divide-y divide-border">
                {s.tools.map((t) => {
                  const ok = toolAllowed(t.name, s.tool_allow);
                  return (
                    <tr key={t.name}>
                      <td className="px-3 py-1.5 font-mono text-xs align-top whitespace-nowrap">{t.name}</td>
                      <td className="px-3 py-1.5 text-fg-muted align-top">{t.description || "—"}</td>
                      <td className="px-3 py-1.5 align-top">{ok ? <Badge tone="success">Yes</Badge> : <Badge>No</Badge>}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="text-sm text-fg-subtle">Not discovered yet. Run Discover tools to connect and list them.</p>
        )}
      </div>
      <div className="flex flex-wrap items-center gap-2 pt-3 border-t border-border">
        <Switch object={s.name} checked={s.enabled} onChange={toggle} disabled={update.isPending} />
        <div className="flex-1" />
        <Button size="sm" icon={RefreshCw} loading={discover.isPending} onClick={run}>Discover tools</Button>
        <IconButton label={`Edit ${s.name}`} icon={Pencil} onClick={onEdit} />
        <IconButton label={`Delete ${s.name}`} icon={Trash2} variant="danger-ghost" onClick={onDelete} />
      </div>
    </Card>
  );
}

// ------------------------------------------------------------------ page
export default function McpServers() {
  const q = useMcpServers();
  const del = useDeleteMcp();
  const user = useSession((s) => s.user);
  const [dialog, setDialog] = useState<{ edit?: McpServerInfo } | null>(null);
  const [deleting, setDeleting] = useState<McpServerInfo | null>(null);
  const servers = q.data?.servers ?? [];
  const stdioAllowed = Boolean(q.data?.stdio_enabled && user?.is_admin);
  const snippet = `claude mcp add --transport http argus ${origin()}/mcp \\\n  --header "Authorization: Bearer <your API key>"`;

  return (
    <ConnectionsPage>
      <PageHeader title="MCP servers" description="Give councils tools from Model Context Protocol servers, and let other MCP clients use Argus."
        actions={<Button variant="primary" icon={Plus} onClick={() => setDialog({})} disabled={q.isLoading}>Add server</Button>} />

      <div className="flex flex-col gap-8">
        <section aria-labelledby="servers-h">
          <SectionTitle id="servers-h">Connected servers</SectionTitle>
          {q.isLoading ? (
            <CardSkeletons count={2} className="md:grid-cols-1 lg:grid-cols-2" />
          ) : q.error ? (
            <Card><ErrorState error={q.error} onRetry={() => void q.refetch()} /></Card>
          ) : servers.length ? (
            <div className="grid gap-4 lg:grid-cols-2 items-start">
              {servers.map((s) => <ServerCard key={s.id} s={s} onEdit={() => setDialog({ edit: s })} onDelete={() => { del.reset(); setDeleting(s); }} />)}
            </div>
          ) : (
            <Card as="div">
              <EmptyState icon={Wrench} title="No MCP servers yet"
                body="Add a Streamable HTTP server (URL plus an optional auth header) to give council members its tools."
                action={<Button variant="primary" icon={Plus} onClick={() => setDialog({})}>Add server</Button>} />
            </Card>
          )}
        </section>

        <section aria-labelledby="argus-mcp-h">
          <SectionTitle id="argus-mcp-h" description="Argus is also an MCP server. Point Claude Code or any MCP client at it.">Use Argus from Claude Code</SectionTitle>
          <Card className="p-4 flex flex-col gap-3">
            <CopyBlock label="Claude Code command" value={snippet} />
            <p className="text-sm text-fg-muted">
              Replace <code className="font-mono text-xs bg-surface-2 rounded-sm px-1">&lt;your API key&gt;</code> with a scoped key from{" "}
              <Link to="/settings/api-keys" className="text-accent underline underline-offset-2">Settings › API keys</Link>.
              The endpoint is <span className="font-mono text-xs">{origin()}/mcp</span>.
            </p>
            <Callout tone="warning" title="Keep keys out of shared shells">
              Anyone with the key can start runs as you. Use a key with only the scopes the client needs, and revoke it when done.
            </Callout>
          </Card>
        </section>
      </div>

      {q.data && !q.data.stdio_enabled && servers.length > 0 && (
        <p className="mt-6 text-xs text-fg-subtle flex items-center gap-1"><ShieldAlert className="size-3.5" aria-hidden />stdio servers are disabled on this deployment.</p>
      )}

      {dialog && (
        <McpDialog key={dialog.edit?.id ?? "new"} open onOpenChange={(o) => !o && setDialog(null)} existing={dialog.edit}
          stdioAllowed={stdioAllowed} takenNames={servers.map((s) => s.name)} />
      )}
      {deleting && (
        <ConfirmDelete open onOpenChange={(o) => !o && setDeleting(null)} kind="MCP server" name={deleting.name}
          consequence={<>Councils lose access to {deleting.tools.length ? `the ${deleting.tools.length} tools` : "any tools"} from <strong>{deleting.name}</strong>. Stored {deleting.transport === "stdio" ? "environment values" : "headers"} are destroyed.</>}
          pending={del.isPending} error={del.error}
          onConfirm={() => del.mutate(deleting.id, { onSuccess: () => { toast({ tone: "success", title: `Deleted ${deleting.name}` }); setDeleting(null); } })} />
      )}
    </ConnectionsPage>
  );
}
