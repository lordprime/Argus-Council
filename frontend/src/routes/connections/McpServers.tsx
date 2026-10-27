import { Ban, Globe, KeyRound, Pencil, PlugZap, Plus, Server, ShieldAlert, Terminal, Trash2, Wrench } from "lucide-react";
import { useRef, useState, type FormEvent } from "react";
import { Link } from "react-router";

import { ConnectionsPage } from "@/components/connections/ConnectionsNav";
import { ConfirmDelete } from "@/components/connections/ConfirmDelete";
import { CopyBlock } from "@/components/connections/CopyBlock";
import { TestSummary, ToolTable } from "@/components/connections/McpTools";
import { CardSkeletons, Chips, Meta, rowsToDict, SecretRows, SectionTitle, splitList, type KV, Switch } from "@/components/connections/parts";
import { TRUST, TrustBadge, TrustControl } from "@/components/connections/TrustControl";
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
import { isAdminUser, type SessionUser } from "@/features/admin/queries";
import {
  trustOf,
  useCreateMcp,
  useDeleteMcp,
  useMcpConnectionTest,
  useUpdateMcp,
  type McpPatch,
  type McpServer,
  type McpTrust,
} from "@/features/connections/queries";
import { errorMessage } from "@/lib/api";
import { cn } from "@/lib/cn";
import { useMcpServers } from "@/lib/queries";
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

function origin() {
  return typeof window === "undefined" ? "https://argus.example" : window.location.origin;
}

// ------------------------------------------------------------------ dialog
function McpDialog({ open, onOpenChange, existing, stdioAllowed, stdioDisabledNote, takenNames }: {
  open: boolean;
  onOpenChange: (o: boolean) => void;
  existing?: McpServer;
  stdioAllowed: boolean;
  /** Shown to admins when the deployment disables stdio; non-admins never see stdio at all. */
  stdioDisabledNote: boolean;
  takenNames: string[];
}) {
  const create = useCreateMcp();
  const update = useUpdateMcp();
  const test = useMcpConnectionTest();
  // After "Add server" the dialog stays open on the new server: it is tested and its tools listed for review.
  const [server, setServer] = useState<McpServer | undefined>(existing);
  const mutation = server ? update : create;
  const [name, setName] = useState(existing?.name ?? "");
  const [transport, setTransport] = useState<"http" | "stdio">(existing?.transport ?? "http");
  const [url, setUrl] = useState(existing?.url ?? "");
  const [command, setCommand] = useState(existing?.command.join("\n") ?? "");
  const [headers, setHeaders] = useState<KV[]>(existing ? [] : [{ key: "Authorization", value: "" }]);
  const [replaceHeaders, setReplaceHeaders] = useState(!existing);
  const [clearHeaders, setClearHeaders] = useState(false);
  const [toolAllow, setToolAllow] = useState((existing?.tool_allow ?? ["*"]).join(", "));
  const [risk, setRisk] = useState<string[]>(existing?.risk ?? ["network"]);
  const [trust, setTrust] = useState<McpTrust>(existing ? trustOf(existing) : "ask");
  const [requireApproval, setRequireApproval] = useState<string[]>(existing?.require_approval ?? []);
  const [enabled, setEnabled] = useState(existing?.enabled ?? true);
  const [touched, setTouched] = useState(false);
  const toolsHeadingRef = useRef<HTMLHeadingElement>(null);

  const errors: Record<string, string> = {};
  if (!server) {
    if (!NAME_RE.test(name)) errors.name = "Lowercase letters, digits, '-' or '_' (max 40)";
    else if (takenNames.includes(name)) errors.name = `A server named ${name} already exists`;
  }
  if (transport === "http" && !/^https?:\/\/\S+$/i.test(url.trim())) errors.url = "Enter the server's http(s) URL";
  if (transport === "stdio" && !command.trim()) errors.command = "Enter the command to run";
  const invalid = Object.keys(errors).length > 0;
  const show = (k: string) => (touched ? errors[k] : undefined);
  const secretsLabel = transport === "stdio" ? "Environment variables" : "Headers";
  const allowList = splitList(toolAllow);
  const tools = test.data?.ok ? test.data.tools : (server?.tools ?? []);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setTouched(true);
    if (invalid) return;
    try {
      if (server) {
        const patch: McpPatch = { tool_allow: allowList, risk, trust, require_approval: requireApproval, enabled };
        if (server.transport === "http") patch.url = url.trim();
        if (clearHeaders) patch.headers = {};
        else if (replaceHeaders && Object.keys(rowsToDict(headers)).length) patch.headers = rowsToDict(headers);
        await update.mutateAsync({ id: server.id, patch });
        toast({ tone: "success", title: `Saved ${server.name}` });
        onOpenChange(false);
      } else {
        const created = await create.mutateAsync({
          name, transport, enabled, risk, trust, require_approval: requireApproval,
          tool_allow: allowList.length ? allowList : ["*"],
          ...(transport === "http" ? { url: url.trim() } : { command: command.split("\n").map((s) => s.trim()).filter(Boolean) }),
          headers: rowsToDict(headers),
        });
        toast({ tone: "success", title: `Added ${name}`, body: "Testing the connection…" });
        setServer(created);
        setReplaceHeaders(false);
        setHeaders([]);
        create.reset();
        test.mutate(created.id);
        requestAnimationFrame(() => toolsHeadingRef.current?.focus());
      }
    } catch {
      /* shown below */
    }
  };

  const title = server ? (existing ? `Edit ${server.name}` : `Review ${server.name}`) : "Add an MCP server";
  return (
    <Dialog open={open} onOpenChange={onOpenChange} wide title={title}
      description="Tools from this server become available to council members, subject to its trust level and guardrails.">
      <form onSubmit={submit} noValidate className="flex flex-col gap-4 min-w-0">
        {touched && invalid && <Callout tone="danger" title="Fix the highlighted fields">{Object.values(errors).join(" · ")}</Callout>}
        {!server && (
          <div className="grid gap-4 sm:grid-cols-2">
            <TextField label="Name" value={name} onChange={(e) => setName(e.target.value)} maxLength={40} spellCheck={false}
              hint="Tools appear as name.tool" error={show("name")} />
            {stdioAllowed ? (
              <Segmented label="Transport" value={transport} onChange={setTransport} options={[
                { value: "http", label: "Streamable HTTP", icon: Globe },
                { value: "stdio", label: "stdio", hint: "Runs a local command on the Argus host (administrators only)", icon: Terminal },
              ]} />
            ) : stdioDisabledNote ? (
              <p className="text-xs text-fg-subtle sm:mt-7">stdio servers are disabled on this deployment.</p>
            ) : null}
          </div>
        )}
        {transport === "http" ? (
          <TextField label="Server URL" type="url" inputMode="url" value={url} onChange={(e) => setUrl(e.target.value)}
            placeholder="https://mcp.example.com/mcp" error={show("url")} spellCheck={false} />
        ) : (
          <TextArea label="Command" value={command} onChange={(e) => setCommand(e.target.value)} disabled={Boolean(server)}
            placeholder={"npx\n-y\n@modelcontextprotocol/server-filesystem\n/data"} className="font-mono" rows={4}
            hint={server ? "The command cannot be changed. Delete and re-add the server instead." : "One argument per line."}
            error={show("command")} spellCheck={false} />
        )}

        {server && !replaceHeaders ? (
          <div className="flex flex-col gap-1">
            <span className="text-sm font-medium">{secretsLabel}</span>
            <div className="flex flex-wrap items-center gap-2">
              {server.header_names.length ? (
                <ul aria-label={`Stored ${secretsLabel.toLowerCase()}`} className="flex flex-wrap gap-1">
                  {server.header_names.map((h) => (
                    <li key={h} className={`inline-flex items-center gap-1 h-7 rounded-sm border border-border bg-surface-2 px-2 font-mono text-xs ${clearHeaders ? "line-through text-fg-subtle" : "text-fg-muted"}`}>
                      <KeyRound className="size-3" aria-hidden />{h}: ••••••
                    </li>
                  ))}
                </ul>
              ) : <span className="text-sm text-fg-subtle">None stored</span>}
              <Button type="button" size="sm" onClick={() => { setReplaceHeaders(true); setClearHeaders(false); setHeaders([{ key: "", value: "" }]); }}>
                Replace all
              </Button>
              {server.header_names.length > 0 && (
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
            hint={server ? "These replace every stored value. Values are encrypted and never shown again." : "Values are encrypted and never shown again."} />
        )}

        <TrustControl value={trust} onChange={setTrust} />

        <section aria-labelledby="mcp-dialog-tools" className="flex flex-col gap-2 min-w-0">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h3 id="mcp-dialog-tools" ref={toolsHeadingRef} tabIndex={-1} className="text-sm font-medium outline-none">
              Tools {server && <span className="text-fg-subtle font-normal">({tools.length})</span>}
            </h3>
            {server && (
              <Button type="button" size="sm" icon={PlugZap} loading={test.isPending} onClick={() => test.mutate(server.id)}>
                Test connection
              </Button>
            )}
          </div>
          {!server ? (
            <p className="text-sm text-fg-subtle">After you add the server, Argus tests the connection and lists its tools here so you can choose which to allow and which always need approval.</p>
          ) : (
            <>
              {test.isPending && <p role="status" className="text-sm text-fg-muted">Connecting and listing tools…</p>}
              {test.data && !test.isPending && <TestSummary result={test.data} />}
              {tools.length ? (
                <ToolTable server={server.name} tools={tools}
                  settings={{ tool_allow: allowList, require_approval: requireApproval, risk, trust }}
                  onChange={(p) => {
                    if (p.tool_allow) setToolAllow(p.tool_allow.join(", "));
                    if (p.require_approval) setRequireApproval(p.require_approval);
                  }} />
              ) : !test.isPending && (
                <p className="text-sm text-fg-subtle">No tools discovered yet. Test the connection to list them.</p>
              )}
              <p className="text-xs text-fg-subtle">
                Tests use the saved settings. Discovery only keeps allowed tools, so a tool you disallow disappears after the next test.
              </p>
            </>
          )}
        </section>

        <TextField label="Allowed tools" value={toolAllow} onChange={(e) => setToolAllow(e.target.value)} spellCheck={false}
          hint="Comma-separated names or patterns. * allows every tool the server offers; leave empty to allow none." />
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
        <div className="flex flex-wrap justify-end gap-2">
          <Button type="button" onClick={() => onOpenChange(false)}>{server && !existing ? "Done" : "Cancel"}</Button>
          <Button type="submit" variant="primary" loading={mutation.isPending}>{server ? "Save" : "Add server"}</Button>
        </div>
      </form>
    </Dialog>
  );
}

// ------------------------------------------------------------------ server card
function ServerCard({ s, onEdit, onDelete }: { s: McpServer; onEdit: () => void; onDelete: () => void }) {
  const test = useMcpConnectionTest();
  const update = useUpdateMcp();
  const headingId = `mcp-${s.id}`;
  const trust = trustOf(s);
  const blocked = trust === "blocked";
  const toggle = (enabled: boolean) =>
    update.mutate({ id: s.id, patch: { enabled } }, {
      onError: (e) => toast({ tone: "danger", title: `Could not update ${s.name}`, body: errorMessage(e) }),
    });

  return (
    <Card as="article" aria-labelledby={headingId}
      className={cn("p-4 flex flex-col gap-3 min-w-0", blocked && "border-dashed border-danger/60 bg-surface-2")}>
      <div className="flex flex-wrap items-start gap-3">
        <span className={cn("grid place-items-center size-9 rounded-md shrink-0", blocked ? "bg-danger-subtle text-danger" : "bg-accent-subtle text-accent")}>
          {blocked ? <Ban className="size-5" aria-hidden /> : s.transport === "stdio" ? <Terminal className="size-5" aria-hidden /> : <Server className="size-5" aria-hidden />}
        </span>
        <div className="min-w-0 flex-1 basis-40">
          <h3 id={headingId} className="font-semibold truncate">{s.name}</h3>
          <p className="text-xs text-fg-subtle font-mono truncate">{s.transport === "http" ? s.url : s.command.join(" ")}</p>
        </div>
        <div className="flex flex-wrap gap-1">
          <TrustBadge trust={trust} />
          <Badge tone={s.transport === "stdio" ? "warning" : "info"}>{s.transport === "stdio" ? "stdio" : "HTTP"}</Badge>
          {!s.enabled && <Badge>Disabled</Badge>}
        </div>
      </div>
      {blocked && (
        <p className="flex items-center gap-1.5 text-sm font-medium text-danger">
          <Ban className="size-4 shrink-0" aria-hidden />Not attached to runs
        </p>
      )}
      <p className="text-xs text-fg-muted">{TRUST[trust].body}</p>
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
      {test.data && <TestSummary result={test.data} />}
      <div className="min-w-0">
        <h4 className="text-sm font-medium mb-1.5">Discovered tools <span className="text-fg-subtle font-normal">({s.tools.length})</span></h4>
        {s.tools.length ? (
          <ToolTable server={s.name} tools={s.tools}
            settings={{ tool_allow: s.tool_allow, require_approval: s.require_approval ?? [], risk: s.risk, trust }} />
        ) : (
          <p className="text-sm text-fg-subtle">Not discovered yet. Test the connection to list its tools.</p>
        )}
      </div>
      <div className="flex flex-wrap items-center gap-2 pt-3 border-t border-border">
        <Switch object={s.name} checked={s.enabled} onChange={toggle} disabled={update.isPending} />
        <div className="flex-1" />
        <Button size="sm" icon={PlugZap} loading={test.isPending} onClick={() => test.mutate(s.id)}
          aria-label={`Test connection to ${s.name}`}>Test connection</Button>
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
  const [dialog, setDialog] = useState<{ edit?: McpServer } | null>(null);
  const [deleting, setDeleting] = useState<McpServer | null>(null);
  const servers: McpServer[] = q.data?.servers ?? [];
  const isAdmin = isAdminUser(user as SessionUser | null);
  const stdioAllowed = Boolean(q.data?.stdio_enabled && isAdmin);
  const snippet = `claude mcp add --transport http argus ${origin()}/mcp \\\n  --header "Authorization: Bearer <your API key>"`;

  return (
    <ConnectionsPage title="MCP servers" description="Give councils tools from Model Context Protocol servers, and let other MCP clients use Argus."
      actions={<Button variant="primary" icon={Plus} onClick={() => setDialog({})} disabled={q.isLoading}>Add server</Button>}>

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
              <EmptyState titleAs="h3" icon={Wrench} title="No MCP servers yet"
                body="Add a Streamable HTTP server (URL plus an optional auth header) to give council members its tools. New servers ask before every tool call until you trust them." />
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

      {isAdmin && q.data && !q.data.stdio_enabled && servers.length > 0 && (
        <p className="mt-6 text-xs text-fg-subtle flex items-center gap-1"><ShieldAlert className="size-3.5" aria-hidden />stdio servers are disabled on this deployment.</p>
      )}

      {dialog && (
        <McpDialog key={dialog.edit?.id ?? "new"} open onOpenChange={(o) => !o && setDialog(null)} existing={dialog.edit}
          stdioAllowed={stdioAllowed} stdioDisabledNote={isAdmin && !q.data?.stdio_enabled} takenNames={servers.map((s) => s.name)} />
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
