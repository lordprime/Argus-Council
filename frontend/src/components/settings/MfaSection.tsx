// Two-factor authentication (TOTP) settings: enrol with an authenticator app, keep recovery codes,
// regenerate them, or turn the second factor off. Nothing is fetched until the user acts, so the
// page loads with the status from GET /auth/me (`mfa_enabled`).

import { useQueryClient } from "@tanstack/react-query";
import { ShieldCheck } from "lucide-react";
import { useState, type FormEvent } from "react";

import { CopyBlock } from "@/components/connections/CopyBlock";
import { SettingsSection } from "@/components/settings/SettingsNav";
import { Badge, Button, Callout, PasswordField, TextField } from "@/components/ui";
import { api, ApiError, errorMessage, V1 } from "@/lib/api";
import type { User } from "@/lib/types";
import { meKey } from "@/features/settings/queries";
import { useSession } from "@/stores/session";
import { toast } from "@/stores/toast";

type Step =
  | { kind: "idle" }
  | { kind: "password" }
  | { kind: "scan"; secret: string; uri: string }
  | { kind: "codes"; codes: string[] }
  | { kind: "manage"; action: "disable" | "regenerate" };

function RecoveryCodes({ codes, onDone }: { codes: string[]; onDone: () => void }) {
  return (
    <div className="flex flex-col gap-3">
      <Callout tone="warning" title="Save your recovery codes now">
        Each code signs you in once if you lose your authenticator. They are not shown again.
      </Callout>
      <CopyBlock label="recovery codes" value={codes.join("\n")} />
      <Button type="button" variant="primary" className="self-start" onClick={onDone}>I saved them</Button>
    </div>
  );
}

export function MfaSection({ user }: { user: User }) {
  const qc = useQueryClient();
  const [step, setStep] = useState<Step>({ kind: "idle" });
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [error, setError] = useState<string | undefined>();
  const [busy, setBusy] = useState(false);
  const enabled = Boolean(user.mfa_enabled);

  const refresh = async () => {
    const me = await api.get<User>(`${V1}/auth/me`);
    qc.setQueryData(meKey, me);
    useSession.setState({ user: me });
  };
  const reset = () => {
    setStep({ kind: "idle" });
    setPassword("");
    setCode("");
    setError(undefined);
  };
  const run = async (fn: () => Promise<void>) => {
    setBusy(true);
    setError(undefined);
    try {
      await fn();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const startSetup = (e: FormEvent) => {
    e.preventDefault();
    void run(async () => {
      const res = await api.post<{ secret: string; otpauth_uri: string }>(`${V1}/auth/mfa/setup`, { password });
      setPassword("");
      setStep({ kind: "scan", secret: res.secret, uri: res.otpauth_uri });
    });
  };
  const confirm = (e: FormEvent) => {
    e.preventDefault();
    void run(async () => {
      const res = await api.post<{ recovery_codes: string[] }>(`${V1}/auth/mfa/enable`, { code: code.trim() });
      setCode("");
      setStep({ kind: "codes", codes: res.recovery_codes });
      await refresh();
      toast({ tone: "success", title: "Two-factor authentication is on", body: "Your other sessions were signed out." });
    });
  };
  const manage = (action: "disable" | "regenerate") => (e: FormEvent) => {
    e.preventDefault();
    void run(async () => {
      const body = { password, code: code.trim() };
      if (action === "disable") {
        await api.post(`${V1}/auth/mfa/disable`, body);
        reset();
        await refresh();
        toast({ tone: "success", title: "Two-factor authentication is off" });
      } else {
        const res = await api.post<{ recovery_codes: string[] }>(`${V1}/auth/mfa/recovery-codes`, body);
        setPassword("");
        setCode("");
        setStep({ kind: "codes", codes: res.recovery_codes });
      }
    });
  };

  const badge = enabled
    ? <Badge tone="success"><ShieldCheck className="size-3" aria-hidden />On</Badge>
    : <Badge tone="neutral">Off</Badge>;

  return (
    <SettingsSection id="two-factor" title="Two-factor authentication" badge={badge}
      description="Ask for a code from an authenticator app (1Password, Aegis, Google Authenticator…) after your password.">
      {error && <Callout tone="danger" title="That did not work">{error}</Callout>}

      {step.kind === "codes" && <RecoveryCodes codes={step.codes} onDone={reset} />}

      {!enabled && step.kind === "idle" && (
        <Button type="button" variant="primary" className="self-start" onClick={() => setStep({ kind: "password" })}>
          Set up two-factor authentication
        </Button>
      )}

      {!enabled && step.kind === "password" && (
        <form onSubmit={startSetup} className="flex flex-col gap-3 sm:max-w-sm" noValidate>
          <PasswordField label="Confirm your password" autoComplete="current-password" required value={password}
            onChange={(e) => setPassword(e.target.value)} />
          <div className="flex gap-2">
            <Button type="submit" variant="primary" loading={busy} disabled={!password}>Continue</Button>
            <Button type="button" variant="ghost" onClick={reset}>Cancel</Button>
          </div>
        </form>
      )}

      {!enabled && step.kind === "scan" && (
        <form onSubmit={confirm} className="flex flex-col gap-3" noValidate>
          <p className="text-sm text-fg-muted leading-6">
            In your authenticator app, add an account with this setup key (time-based, 6 digits), or open the
            link on a device that has the app. Then enter the code it shows.
          </p>
          <CopyBlock label="setup key" value={step.secret} />
          <a className="text-sm text-accent underline self-start" href={step.uri}>Open in authenticator app</a>
          <TextField label="Code from the app" autoComplete="one-time-code" inputMode="numeric" maxLength={8}
            className="sm:max-w-xs" value={code} onChange={(e) => setCode(e.target.value)} />
          <div className="flex gap-2">
            <Button type="submit" variant="primary" loading={busy} disabled={code.trim().length < 6}>Turn on</Button>
            <Button type="button" variant="ghost" onClick={reset}>Cancel</Button>
          </div>
        </form>
      )}

      {enabled && step.kind === "idle" && (
        <div className="flex flex-wrap gap-2">
          <Button type="button" onClick={() => setStep({ kind: "manage", action: "regenerate" })}>New recovery codes</Button>
          <Button type="button" variant="danger" onClick={() => setStep({ kind: "manage", action: "disable" })}>
            Turn off
          </Button>
        </div>
      )}

      {enabled && step.kind === "manage" && (
        <form onSubmit={manage(step.action)} className="flex flex-col gap-3 sm:max-w-sm" noValidate>
          <PasswordField label="Password" autoComplete="current-password" required value={password}
            onChange={(e) => setPassword(e.target.value)} />
          <TextField label={step.action === "disable" ? "Code from the app, or a recovery code" : "Code from the app"}
            autoComplete="one-time-code" maxLength={32} required value={code} onChange={(e) => setCode(e.target.value)} />
          <div className="flex gap-2">
            <Button type="submit" variant={step.action === "disable" ? "danger" : "primary"} loading={busy}
              disabled={!password || code.trim().length < 6}>
              {step.action === "disable" ? "Turn off two-factor authentication" : "Replace recovery codes"}
            </Button>
            <Button type="button" variant="ghost" onClick={reset}>Cancel</Button>
          </div>
        </form>
      )}
    </SettingsSection>
  );
}
