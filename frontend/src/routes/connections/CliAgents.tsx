// Connections > CLI agents: the vendors' official CLIs (Claude Code, Codex, Gemini CLI, Copilot, Kiro,
// Qwen Code, opencode, Goose, Vibe) as council seats that run with the user's own vendor account or
// a per-user CLI API key. One compact row per CLI; sign-in and keys open behind "Set up".

import { ChevronDown, ChevronUp, KeyRound, RefreshCw, Terminal, UserCheck } from "lucide-react";
import { useState, type FormEvent } from "react";

import { ConnectionsPage } from "@/components/connections/ConnectionsNav";
import { CopyBlock } from "@/components/connections/CopyBlock";
import { CardSkeletons, SectionTitle } from "@/components/connections/parts";
import { Badge, Button, Callout, Card, EmptyState, ErrorState, PasswordField, SelectField, TagList } from "@/components/ui";
import {
  LOGIN_ACTIVE, useCliAgents, useCliLogin, useCliSessions, useDeleteCliKey, useSetCliKey,
} from "@/features/connections/cliAgents";
import { errorMessage } from "@/lib/api";
import type { CliAgentInfo } from "@/lib/types";
import { CliSignIn } from "@/routes/connections/CliSignIn";
import { toast } from "@/stores/toast";

function loginState(c: CliAgentInfo): { tone: "success" | "warning" | "neutral"; text: string } {
  if (c.logged_in === true) {
    const how = c.login_method ? ` (${c.login_method})` : "";
    return { tone: "success", text: c.login_verified ? `Signed in${how}` : `Sign-in found${how}, not yet verified` };
  }
  if (c.logged_in === false) return { tone: "warning", text: "Not signed in" };
  return { tone: "neutral", text: "Sign-in not checked" };
}

/** Model providers a harness CLI's key can be for (labels for the key form). */
const PROVIDER_LABELS: Record<string, string> = {
  anthropic: "Anthropic", openai: "OpenAI", google: "Google (Gemini API)", openrouter: "OpenRouter", mistral: "Mistral",
  dashscope: "Alibaba Model Studio (DashScope, pay-as-you-go)",
};

function KeyForm({ c }: { c: CliAgentInfo }) {
  const setKey = useSetCliKey();
  const delKey = useDeleteCliKey();
  const [value, setValue] = useState("");
  const [editing, setEditing] = useState(false);
  const providers = c.key_providers ?? [];
  const [provider, setProvider] = useState(c.key_provider ?? providers[0] ?? "");
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (value.trim().length < 8) return;
    setKey.mutate({ cli: c.cli, apiKey: value.trim(), provider: providers.length ? provider : null }, {
      onSuccess: () => { setValue(""); setEditing(false); toast({ tone: "success", title: `${c.label} key saved` }); },
      onError: (err) => toast({ tone: "danger", title: "Could not save the key", body: errorMessage(err) }),
    });
  };
  if (c.has_api_key && !editing) {
    return (
      <div className="flex flex-wrap items-center gap-2">
        <span className="inline-flex items-center gap-2 h-9 rounded-md border border-border bg-surface-2 px-3 text-sm text-fg-muted">
          <KeyRound className="size-4" aria-hidden />Stored · write-only{c.key_provider ? ` · ${PROVIDER_LABELS[c.key_provider] ?? c.key_provider}` : ""}{c.key_hint ? ` · ${c.key_hint}` : ""}
        </span>
        <Button size="sm" onClick={() => setEditing(true)}>Replace</Button>
        <Button size="sm" variant="danger-ghost" loading={delKey.isPending}
          onClick={() => delKey.mutate(c.cli, { onError: (err) => toast({ tone: "danger", title: "Could not remove the key", body: errorMessage(err) }) })}>
          Remove
        </Button>
      </div>
    );
  }
  return (
    <form className="flex flex-col gap-2" onSubmit={submit} aria-label={`${c.label} API key`}>
      {providers.length > 0 && (
        <SelectField label="Key is for" value={provider} onChange={(e) => setProvider(e.target.value)}>
          {providers.map((p) => <option key={p} value={p}>{PROVIDER_LABELS[p] ?? p}</option>)}
        </SelectField>
      )}
      <PasswordField label={providers.length ? "API key for this CLI" : `${c.key_env} for this CLI`} value={value} autoComplete="new-password" spellCheck={false}
        hint="Optional. Used instead of your sign-in, only for your own runs. Stored encrypted, never shown again."
        onChange={(e) => setValue(e.target.value)} />
      <div className="flex gap-2">
        <Button type="submit" size="sm" variant="primary" disabled={value.trim().length < 8} loading={setKey.isPending}>Save key</Button>
        {editing && <Button type="button" size="sm" variant="ghost" onClick={() => { setEditing(false); setValue(""); }}>Cancel</Button>}
      </div>
    </form>
  );
}

/** The one-line status of a CLI for this user (badge text and tone). */
function rowStatus(c: CliAgentInfo, runnerDown: boolean): { tone: "success" | "warning" | "neutral" | "danger"; text: string } {
  if (runnerDown) return { tone: "danger", text: "Runner not running" };
  if (c.installed === false) return { tone: "danger", text: "Not installed" };
  if (c.logged_in === true) return loginState(c);
  if (c.has_api_key) return { tone: "success", text: "Uses your API key" };
  if (c.login_flow === null) return { tone: "warning", text: "Needs an API key" };
  return loginState(c);
}

/** One CLI: a compact summary row; sign-in, key and server command behind "Set up". */
function CliRow({ c, allowSubscription, runnerDown }: { c: CliAgentInfo; allowSubscription: boolean; runnerDown: boolean }) {
  const headingId = `cli-${c.cli}`;
  const panelId = `cli-${c.cli}-panel`;
  const canSignIn = allowSubscription && c.login_flow !== null;
  const login = useCliLogin(c.cli, canSignIn && !runnerDown);
  const signingIn = LOGIN_ACTIVE.has(login.data?.state ?? "none");
  const [open, setOpen] = useState(false);
  const expanded = open || signingIn;
  const status = rowStatus(c, runnerDown);
  const version = runnerDown ? null : c.version;
  return (
    <li>
      <article aria-labelledby={headingId} className="flex flex-col gap-3 px-4 py-3">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
          <Terminal className="size-4 shrink-0 text-fg-muted" aria-hidden />
          <h3 id={headingId} className="font-semibold">{c.label}</h3>
          <Badge tone={status.tone}>{status.text}</Badge>
          {c.has_api_key && c.logged_in === true && <Badge tone="info">API key</Badge>}
          <span className="font-mono text-xs text-fg-subtle">{c.provider}</span>
          {version && <span className="font-mono text-xs text-fg-subtle truncate max-w-48" title={version}>{version}</span>}
          <Button size="sm" variant="ghost" className="ml-auto" icon={expanded ? ChevronUp : ChevronDown}
            aria-expanded={expanded} aria-controls={panelId} onClick={() => setOpen(!expanded)} disabled={signingIn}>
            {expanded ? "Close" : c.logged_in || c.has_api_key ? "Manage" : "Set up"}
          </Button>
        </div>
        <TagList items={c.suggested_models} label={`${c.label} models`} max={3} className="pl-7" />
        {expanded && (
          <div id={panelId} className="grid gap-4 pl-7 lg:grid-cols-2">
            {canSignIn && (
              <section aria-label={`${c.label} account`} className="flex flex-col gap-2 min-w-0">
                <h4 className="text-sm font-medium">Your account</h4>
                {runnerDown ? (
                  <p className="text-sm text-fg-muted">Signing in needs the agent runner, which is not running.</p>
                ) : (
                  <>
                    <p className="text-xs text-fg-subtle">
                      The CLI signs itself in inside your private runner home. Argus shows you its link and code and never sees the login.
                    </p>
                    <CliSignIn c={c} />
                  </>
                )}
                <details className="text-sm">
                  <summary className="cursor-pointer text-fg-muted">Sign in from the server instead</summary>
                  <p className="text-xs text-fg-subtle my-2">An operator runs this on the server; you then complete the CLI's own sign-in.</p>
                  <CopyBlock label={`${c.label} sign-in command`} value={c.login_command.replace("<your-email>", "you@example.com")} />
                </details>
              </section>
            )}
            <section aria-label={`${c.label} API key`} className="flex flex-col gap-2 min-w-0">
              <h4 className="text-sm font-medium">API key</h4>
              <KeyForm c={c} />
            </section>
            {c.suggested_models.length > 3 && (
              <div className="lg:col-span-2 min-w-0">
                <p className="text-xs text-fg-subtle mb-1">All suggested models</p>
                <TagList items={c.suggested_models} label={`All ${c.label} models`} />
              </div>
            )}
            {c.status_detail && !runnerDown && <p className="lg:col-span-2 text-xs text-fg-subtle">{c.status_detail}</p>}
          </div>
        )}
      </article>
    </li>
  );
}

/** CLIs the operator has not offered on this instance, with how to offer them. */
function DisabledClis({ clis, enabled }: { clis: CliAgentInfo[]; enabled: CliAgentInfo[] }) {
  if (!clis.length) return null;
  const example = [...enabled, ...clis.slice(0, 1)].map((c) => c.cli).join(",");
  return (
    <section aria-labelledby="cli-off-h">
      <SectionTitle id="cli-off-h" count={clis.length}
        description="Installed in the runner but not offered on this instance.">Not offered on this instance</SectionTitle>
      <Card className="flex flex-col gap-3">
        <TagList items={clis.map((c) => c.label)} label="CLIs not offered" />
        <p className="text-sm text-fg-muted">
          An operator offers a CLI by adding its name to <code className="font-mono text-xs">ARGUS_CLI_AGENTS_ALLOWED</code> in{" "}
          <code className="font-mono text-xs">deploy/.env</code>, for example{" "}
          <code className="font-mono text-xs break-all">ARGUS_CLI_AGENTS_ALLOWED={example}</code>, then runs{" "}
          <code className="font-mono text-xs">./scripts/deploy.sh</code>. The CLIs added in 0.1.4 are off until then
          (see docs/cli-agents.md).
        </p>
      </Card>
    </section>
  );
}

function Sessions() {
  const q = useCliSessions();
  const rows = q.data ?? [];
  if (q.isLoading || !rows.length) return null;
  return (
    <section aria-labelledby="cli-sessions-h">
      <SectionTitle id="cli-sessions-h" count={rows.length}
        description="Vendor sessions your seats ran in. Pin one on a seat to continue it in a later run.">Your CLI sessions</SectionTitle>
      <Card>
        <ul className="flex flex-col divide-y divide-border">
          {rows.slice(0, 20).map((s) => (
            <li key={s.id} className="py-2 flex flex-wrap gap-x-3 text-sm">
              <span className="font-mono text-xs">{s.cli}</span>
              <span className="font-mono text-xs break-all">{s.session_id}</span>
              {s.label && <span>{s.label}</span>}
              <span className="text-fg-subtle text-xs ml-auto">{new Date(s.last_used_at).toLocaleString()}</span>
            </li>
          ))}
        </ul>
      </Card>
    </section>
  );
}

export default function CliAgents() {
  const q = useCliAgents();
  const data = q.data;
  const runnerDown = Boolean(data?.runner_error);
  const offered = data?.clis.filter((c) => c.enabled) ?? [];
  const notOffered = data?.clis.filter((c) => !c.enabled) ?? [];
  return (
    <ConnectionsPage title="CLI agents"
      description="Seat a vendor's coding CLI (Claude Code, Codex, Gemini CLI and more) on a council, signed in with your own account or a CLI API key."
      actions={<Button icon={RefreshCw} onClick={() => void q.refetch()} loading={q.isFetching}>Refresh status</Button>}>
      <div className="flex flex-col gap-6">
        <Callout tone="warning" title="Use within each vendor's terms">
          {data?.notice ?? "Each vendor's terms govern how its CLI may be used. Subscription logins are for the account owner's own use; on an instance shared by several people, use API keys."}
        </Callout>
        {q.isLoading ? (
          <CardSkeletons count={3} />
        ) : q.error ? (
          <Card><ErrorState error={q.error} onRetry={() => void q.refetch()} /></Card>
        ) : !data?.enabled ? (
          <Card>
            {data?.turned_off ? (
              <EmptyState icon={UserCheck} title="CLI agents are turned off"
                body="An admin turned CLI agents off in Settings → Features. Your sign-ins and API keys are kept for when they are turned back on." />
            ) : (
              <EmptyState icon={UserCheck} title="CLI agents are off on this instance"
                body="An operator installs them on the server with ./scripts/deploy.sh --with cli-agents (see docs/cli-agents.md)." />
            )}
          </Card>
        ) : (
          <>
            {runnerDown && (
              <Callout tone="danger" title="The agent runner is not running">
                Sign-in, status and CLI seats need it; saved API keys are kept. An operator starts it with{" "}
                <code className="font-mono text-xs">./scripts/deploy.sh --with cli-agents</code>. ({data.runner_error})
              </Callout>
            )}
            {!data.allow_subscription && (
              <Callout tone="info" title="API keys only">This instance runs CLI agents with each user's own API key; account sign-ins are disabled.</Callout>
            )}
            <section aria-labelledby="cli-list-h">
              <SectionTitle id="cli-list-h" count={offered.length}
                description="Seats on a CLI agent show an Account badge. Give them a fallback route (for example the same vendor's API key provider) for when the account hits its usage limit.">
                Available CLIs
              </SectionTitle>
              <Card className="p-0">
                <ul className="flex flex-col divide-y divide-border">
                  {offered.map((c) => <CliRow key={c.cli} c={c} allowSubscription={data.allow_subscription} runnerDown={runnerDown} />)}
                </ul>
              </Card>
            </section>
            <DisabledClis clis={notOffered} enabled={offered} />
            <Sessions />
          </>
        )}
      </div>
    </ConnectionsPage>
  );
}
