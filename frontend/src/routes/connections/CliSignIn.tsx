// In-app sign-in for one CLI agent: the runner starts the CLI's own login in the user's runner home;
// this panel shows only what the CLI printed for the user to act on (a link, a one-time code) and
// passes a pasted authorization code straight back to the CLI. Nothing here stores the code.

import { ExternalLink, LogIn, LogOut } from "lucide-react";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { CopyBlock } from "@/components/connections/CopyBlock";
import { Button, Callout, PasswordField, SelectField } from "@/components/ui";
import {
  LOGIN_ACTIVE, cliKeys, useCancelCliLogin, useCliLogin, useCliLogout, useStartCliLogin, useSubmitCliLoginCode,
} from "@/features/connections/cliAgents";
import { errorMessage } from "@/lib/api";
import type { CliAgentInfo, CliLoginState } from "@/lib/types";
import { toast } from "@/stores/toast";

/** Whose account each CLI signs in with (the button says so). */
const ACCOUNT: Record<string, string> = {
  claude: "Claude account", codex: "ChatGPT account", gemini: "Google account",
  copilot: "GitHub account", kiro: "Kiro account", opencode: "account",
};
const METHOD_LABELS: Record<string, string> = {
  builder_id: "AWS Builder ID", google: "Google", github: "GitHub", github_copilot: "GitHub Copilot", chatgpt: "ChatGPT Plus/Pro",
};
const STATUS_TEXT: Record<string, string> = {
  starting: "Starting the CLI's sign-in…",
  waiting: "Waiting for you to finish on the sign-in page…",
  verifying: "Checking the code with the CLI…",
};

const hostOf = (url: string) => {
  try { return new URL(url).host; } catch { return url; }
};

function Steps({ c, st }: { c: CliAgentInfo; st: CliLoginState }) {
  const submit = useSubmitCliLoginCode(c.cli);
  const [code, setCode] = useState("");
  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    const value = code.trim();
    if (!value) return;
    submit.mutate(value, {
      onSuccess: () => setCode(""),
      onError: (err) => toast({ tone: "danger", title: "Could not pass the code to the CLI", body: errorMessage(err) }),
    });
  };
  return (
    <ol className="flex flex-col gap-3 text-sm list-decimal pl-5">
      {st.verification_url && (
        <li>
          <span className="block mb-1">Open the sign-in page and sign in with your {ACCOUNT[c.cli] ?? "account"}:</span>
          <a href={st.verification_url} target="_blank" rel="noopener noreferrer"
            className="inline-flex items-center gap-1.5 font-medium text-accent underline underline-offset-2">
            <ExternalLink className="size-4" aria-hidden />Open {hostOf(st.verification_url)}
          </a>
        </li>
      )}
      {st.user_code && (
        <li>
          <span className="block mb-1">Enter this one-time code on that page. Use it only because you started this sign-in here.</span>
          <CopyBlock label="One-time code" value={st.user_code} />
        </li>
      )}
      {st.flow === "paste_code" && (
        <li>
          <form className="flex flex-col gap-2" onSubmit={onSubmit} aria-label={`${c.label} authorization code`}>
            <PasswordField label="Code shown after you sign in" value={code} autoComplete="off" spellCheck={false}
              hint="Passed straight to the CLI. Argus does not store it."
              disabled={!st.needs_code} onChange={(e) => setCode(e.target.value)} />
            <div>
              <Button type="submit" size="sm" variant="primary" disabled={!st.needs_code || !code.trim()} loading={submit.isPending}>
                Finish sign-in
              </Button>
            </div>
          </form>
        </li>
      )}
    </ol>
  );
}

export function CliSignIn({ c }: { c: CliAgentInfo }) {
  const qc = useQueryClient();
  const login = useCliLogin(c.cli, c.enabled && c.login_flow !== null);
  const start = useStartCliLogin(c.cli);
  const cancel = useCancelCliLogin(c.cli);
  const logout = useCliLogout(c.cli);
  const [method, setMethod] = useState(c.login_methods[0] ?? "");
  const st = login.data;
  const active = st ? LOGIN_ACTIVE.has(st.state) : false;

  // Refresh the card's signed-in badge once a sign-in completes.
  const seen = useRef<string | null>(null);
  useEffect(() => {
    if (st?.state === "succeeded" && seen.current !== "succeeded") void qc.invalidateQueries({ queryKey: cliKeys.overview });
    seen.current = st?.state ?? null;
  }, [st?.state, qc]);

  if (c.login_flow === null) return null;
  const begin = () =>
    start.mutate(c.login_methods.length ? method : null, {
      onError: (err) => toast({ tone: "danger", title: `Could not start the ${c.label} sign-in`, body: errorMessage(err) }),
    });
  const signOut = () => {
    if (!window.confirm(`Sign ${c.label} out of your ${ACCOUNT[c.cli] ?? "account"}? Seats on it stop working until you sign in again.`)) return;
    logout.mutate(undefined, {
      onSuccess: () => toast({ tone: "success", title: `${c.label} signed out` }),
      onError: (err) => toast({ tone: "danger", title: "Could not sign out", body: errorMessage(err) }),
    });
  };

  if (st && active) {
    return (
      <div role="group" aria-label={`${c.label} sign-in`} className="flex flex-col gap-3 rounded-md border border-border p-3">
        {st.retry && (
          <Callout tone="warning" title="That code was not accepted">The CLI showed a new link. Open it and use the new code.</Callout>
        )}
        <Steps c={c} st={st} />
        <div className="flex flex-wrap items-center gap-2">
          <p role="status" className="text-xs text-fg-muted flex-1 min-w-40">
            {STATUS_TEXT[st.state]}
            {st.expires_at ? ` Expires at ${new Date(st.expires_at * 1000).toLocaleTimeString()}.` : ""}
          </p>
          <Button size="sm" variant="ghost" loading={cancel.isPending} onClick={() => cancel.mutate()}>Cancel</Button>
        </div>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-2">
      {st?.state === "succeeded" && <Callout tone="success" title={`${c.label} is signed in`}>The login is stored in your private runner home.</Callout>}
      {st && (st.state === "failed" || st.state === "expired") && (
        <Callout tone="danger" title={st.state === "expired" ? "The sign-in timed out" : "The sign-in failed"}>{st.message}</Callout>
      )}
      <div className="flex flex-wrap items-end gap-2">
        {c.login_methods.length > 0 && (
          <SelectField label="Sign in with" value={method} onChange={(e) => setMethod(e.target.value)}>
            {c.login_methods.map((m) => <option key={m} value={m}>{METHOD_LABELS[m] ?? m}</option>)}
          </SelectField>
        )}
        <Button size="sm" variant={c.logged_in ? "secondary" : "primary"} icon={LogIn} loading={start.isPending} onClick={begin}>
          {c.logged_in ? "Sign in again" : `Sign in with your ${ACCOUNT[c.cli] ?? "account"}`}
        </Button>
        {c.logged_in && (
          <Button size="sm" variant="danger-ghost" icon={LogOut} loading={logout.isPending} onClick={signOut}>Sign out</Button>
        )}
      </div>
    </div>
  );
}
