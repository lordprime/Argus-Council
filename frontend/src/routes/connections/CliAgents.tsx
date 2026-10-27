// Connections > CLI agents: the vendors' official CLIs (Claude Code, Codex, Gemini CLI) as council
// seats that run with the user's own vendor account or a per-user CLI API key.

import { KeyRound, RefreshCw, Terminal, UserCheck } from "lucide-react";
import { useState, type FormEvent } from "react";

import { ConnectionsPage } from "@/components/connections/ConnectionsNav";
import { CopyBlock } from "@/components/connections/CopyBlock";
import { CardSkeletons, Meta, SectionTitle } from "@/components/connections/parts";
import { Badge, Button, Callout, Card, EmptyState, ErrorState, PasswordField } from "@/components/ui";
import { useCliAgents, useCliSessions, useDeleteCliKey, useSetCliKey } from "@/features/connections/cliAgents";
import { errorMessage } from "@/lib/api";
import type { CliAgentInfo } from "@/lib/types";
import { toast } from "@/stores/toast";

function loginState(c: CliAgentInfo): { tone: "success" | "warning" | "neutral"; text: string } {
  if (c.logged_in === true) {
    const how = c.login_method ? ` (${c.login_method})` : "";
    return { tone: "success", text: c.login_verified ? `Signed in${how}` : `Sign-in found${how}, not yet verified` };
  }
  if (c.logged_in === false) return { tone: "warning", text: "Not signed in" };
  return { tone: "neutral", text: "Unknown" };
}

function KeyForm({ c }: { c: CliAgentInfo }) {
  const setKey = useSetCliKey();
  const delKey = useDeleteCliKey();
  const [value, setValue] = useState("");
  const [editing, setEditing] = useState(false);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (value.trim().length < 8) return;
    setKey.mutate({ cli: c.cli, apiKey: value.trim() }, {
      onSuccess: () => { setValue(""); setEditing(false); toast({ tone: "success", title: `${c.label} key saved` }); },
      onError: (err) => toast({ tone: "danger", title: "Could not save the key", body: errorMessage(err) }),
    });
  };
  if (c.has_api_key && !editing) {
    return (
      <div className="flex flex-wrap items-center gap-2">
        <span className="inline-flex items-center gap-2 h-9 rounded-md border border-border bg-surface-2 px-3 text-sm text-fg-muted">
          <KeyRound className="size-4" aria-hidden />Stored · write-only{c.key_hint ? ` · ${c.key_hint}` : ""}
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
      <PasswordField label={`${c.key_env} for this CLI`} value={value} autoComplete="new-password" spellCheck={false}
        hint="Optional. Used instead of your sign-in, only for your own runs. Stored encrypted, never shown again."
        onChange={(e) => setValue(e.target.value)} />
      <div className="flex gap-2">
        <Button type="submit" size="sm" variant="primary" disabled={value.trim().length < 8} loading={setKey.isPending}>Save key</Button>
        {editing && <Button type="button" size="sm" variant="ghost" onClick={() => { setEditing(false); setValue(""); }}>Cancel</Button>}
      </div>
    </form>
  );
}

function CliCard({ c, allowSubscription }: { c: CliAgentInfo; allowSubscription: boolean }) {
  const state = loginState(c);
  const headingId = `cli-${c.cli}`;
  return (
    <Card as="article" aria-labelledby={headingId} className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-2">
        <Terminal className="size-4 text-fg-muted" aria-hidden />
        <h3 id={headingId} className="font-semibold">{c.label}</h3>
        {!c.enabled && <Badge>Disabled on this instance</Badge>}
        {c.enabled && <Badge tone={state.tone}>{state.text}</Badge>}
        {c.has_api_key && <Badge tone="info">API key</Badge>}
      </div>
      <dl className="grid gap-3 sm:grid-cols-3">
        <Meta label="Seat provider" mono>{c.provider}</Meta>
        <Meta label="Installed version" mono>{c.version ?? (c.installed === false ? "Not installed" : "Unknown")}</Meta>
        <Meta label="Models">{c.suggested_models.join(", ")}</Meta>
      </dl>
      {c.status_detail && <p className="text-xs text-fg-subtle">{c.status_detail}</p>}
      {c.enabled && allowSubscription && (
        <div className="flex flex-col gap-2">
          <p className="text-sm text-fg-muted">
            Sign in with your own account: an operator runs this on the server, then you complete the CLI's own sign-in.
            The login stays in your private runner home; Argus never sees the token.
          </p>
          <CopyBlock label={`${c.label} sign-in command`} value={c.login_command.replace("<your-email>", "you@example.com")} />
        </div>
      )}
      {c.enabled && <KeyForm c={c} />}
    </Card>
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
  return (
    <ConnectionsPage title="CLI agents"
      description="Seat Claude Code, the Codex CLI or the Gemini CLI on a council, signed in with your own account or a CLI API key."
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
            <EmptyState icon={UserCheck} title="CLI agents are off on this instance"
              body="An operator turns them on with ARGUS_CLI_AGENTS_ENABLED=true and the cli-agents compose profile (see docs/cli-agents.md)." />
          </Card>
        ) : (
          <>
            {data.runner_error && (
              <Callout tone="danger" title="The agent runner is unreachable">{data.runner_error}</Callout>
            )}
            {!data.allow_subscription && (
              <Callout tone="info" title="API keys only">This instance runs CLI agents with each user's own API key; account sign-ins are disabled.</Callout>
            )}
            <section aria-labelledby="cli-list-h">
              <SectionTitle id="cli-list-h"
                description="Seats on a CLI agent show an Account badge. Give them a fallback route (for example the same vendor's API key provider) for when the account hits its usage limit.">
                Available CLIs
              </SectionTitle>
              <div className="grid gap-4 lg:grid-cols-2 xl:grid-cols-3 items-start">
                {data.clis.map((c) => <CliCard key={c.cli} c={c} allowSubscription={data.allow_subscription} />)}
              </div>
            </section>
            <Sessions />
          </>
        )}
      </div>
    </ConnectionsPage>
  );
}
