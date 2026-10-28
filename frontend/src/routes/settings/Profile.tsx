import { Monitor, Moon, Rows3, Rows4, ShieldCheck, Sun } from "lucide-react";
import { useEffect, useState, type FormEvent } from "react";

import { MfaSection } from "@/components/settings/MfaSection";
import { SettingsLayout, SettingsSection } from "@/components/settings/SettingsNav";
import { Badge, Button, Callout, ErrorState, Kbd, Segmented, Skeleton, TextField, Toggle } from "@/components/ui";
import { ApiError, errorMessage } from "@/lib/api";
import type { User } from "@/lib/types";
import { applyReducedMotion, readReducedMotion, setReducedMotion } from "@/features/settings/motion";
import { useMe, useUpdateMe } from "@/features/settings/queries";
import { toast } from "@/stores/toast";
import { useUi, type Density, type Theme } from "@/stores/ui";

const MIN_PASSWORD = 12;

const THEMES: { value: Theme; label: string; icon: typeof Sun }[] = [
  { value: "system", label: "System", icon: Monitor },
  { value: "light", label: "Light", icon: Sun },
  { value: "dark", label: "Dark", icon: Moon },
];
const DENSITIES: { value: Density; label: string; icon: typeof Sun; hint: string }[] = [
  { value: "comfortable", label: "Comfortable", icon: Rows3, hint: "More spacing between items" },
  { value: "compact", label: "Compact", icon: Rows4, hint: "Fit more on screen" },
];

// ------------------------------------------------------------------ Account
function AccountSection({ user }: { user: User }) {
  const update = useUpdateMe();
  const [name, setName] = useState(user.display_name);
  const dirty = name.trim() !== user.display_name;

  const save = async (e: FormEvent) => {
    e.preventDefault();
    if (!dirty) return;
    try {
      await update.mutateAsync({ display_name: name.trim() });
      toast({ tone: "success", title: "Display name saved" });
    } catch {
      /* shown inline */
    }
  };

  return (
    <form onSubmit={save} aria-labelledby="account-h">
      <SettingsSection id="account" title="Account"
        description={`Member since ${new Date(user.created_at).toLocaleDateString(undefined, { dateStyle: "medium" })}.`}
        badge={user.is_admin ? <Badge tone="accent"><ShieldCheck className="size-3" aria-hidden />Admin</Badge> : undefined}
        footer={
          <>
            {dirty && <Button type="button" variant="ghost" onClick={() => setName(user.display_name)}>Reset</Button>}
            <Button type="submit" variant="primary" loading={update.isPending} disabled={!dirty}>Save name</Button>
          </>
        }>
        <div className="grid gap-4 sm:grid-cols-2">
          <TextField label="Email" type="email" value={user.email} readOnly autoComplete="username"
            hint="Used to sign in. Ask an administrator to change it." />
          <TextField label="Display name" value={name} maxLength={120} autoComplete="nickname"
            hint="Shown on your interventions in run timelines."
            error={update.error ? errorMessage(update.error) : undefined}
            onChange={(e) => setName(e.target.value)} />
        </div>
      </SettingsSection>
    </form>
  );
}

// ------------------------------------------------------------------ Password
type PwErrors = Partial<Record<"current" | "next" | "confirm", string>>;

export function validatePasswordChange(current: string, next: string, confirm: string): PwErrors {
  const errs: PwErrors = {};
  if (!current) errs.current = "Enter your current password.";
  if (next.length < MIN_PASSWORD) errs.next = `Use at least ${MIN_PASSWORD} characters.`;
  else if (next === current) errs.next = "Choose a password different from the current one.";
  if (!confirm) errs.confirm = "Re-enter the new password.";
  else if (confirm !== next) errs.confirm = "Passwords do not match.";
  return errs;
}

function PasswordSection() {
  const update = useUpdateMe();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [errors, setErrors] = useState<PwErrors>({});
  const [submitted, setSubmitted] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setSubmitted(true);
    const errs = validatePasswordChange(current, next, confirm);
    setErrors(errs);
    if (Object.keys(errs).length) return;
    try {
      await update.mutateAsync({ current_password: current, new_password: next });
      setCurrent("");
      setNext("");
      setConfirm("");
      setSubmitted(false);
      toast({ tone: "success", title: "Password changed", body: "Use the new password next time you sign in." });
    } catch (err) {
      if (err instanceof ApiError && (err.status === 400 || err.status === 401 || err.status === 403)) {
        setErrors({ current: err.message || "Current password is incorrect." });
      } else if (err instanceof ApiError && err.status === 422) {
        setErrors({ next: err.message || `Use at least ${MIN_PASSWORD} characters.` });
      } else {
        toast({ tone: "danger", title: "Could not change password", body: errorMessage(err) });
      }
    }
  };

  const errorList = Object.values(errors).filter(Boolean);

  return (
    <form onSubmit={submit} noValidate aria-labelledby="password-h">
      <SettingsSection id="password" title="Password" description="Changing your password keeps you signed in on this device."
        footer={<Button type="submit" variant="primary" loading={update.isPending}>Change password</Button>}>
        {submitted && errorList.length > 0 && (
          <Callout tone="danger" title={`Fix ${errorList.length === 1 ? "1 problem" : `${errorList.length} problems`} to continue`}>
            <ul className="list-disc pl-4">{errorList.map((m) => <li key={m}>{m}</li>)}</ul>
          </Callout>
        )}
        <TextField label="Current password" type="password" autoComplete="current-password" value={current}
          error={errors.current} onChange={(e) => setCurrent(e.target.value)} className="sm:max-w-sm" />
        <div className="grid gap-4 sm:grid-cols-2">
          <TextField label="New password" type="password" autoComplete="new-password" value={next}
            hint={`At least ${MIN_PASSWORD} characters. A short phrase is easier to remember.`}
            error={errors.next} onChange={(e) => setNext(e.target.value)} />
          <TextField label="Confirm new password" type="password" autoComplete="new-password" value={confirm}
            error={errors.confirm} onChange={(e) => setConfirm(e.target.value)} />
        </div>
      </SettingsSection>
    </form>
  );
}

// ------------------------------------------------------------------ Appearance & keyboard
function AppearanceSection() {
  const { theme, density, setTheme, setDensity } = useUi();
  const [reduced, setReduced] = useState(readReducedMotion);
  useEffect(() => applyReducedMotion(readReducedMotion()), []);

  return (
    <SettingsSection id="appearance" title="Appearance" description="Saved in this browser.">
      {/* Keep the segmented controls at their natural width instead of stretching across the card. */}
      <div className="flex flex-col gap-4 [&_[role=radiogroup]]:self-start">
        <Segmented label="Theme" value={theme} onChange={setTheme} options={THEMES} />
        <Segmented label="Density" value={density} onChange={setDensity} options={DENSITIES} />
      </div>
      <Toggle label="Reduce motion" checked={reduced}
        hint="Turns off animations and transitions even if your system allows them. Your system setting is always respected."
        onChange={(on) => {
          setReduced(on);
          setReducedMotion(on);
        }} />
    </SettingsSection>
  );
}

function KeyboardSection() {
  const { shortcuts, setShortcuts } = useUi();
  return (
    <SettingsSection id="keyboard" title="Keyboard">
      <Toggle label="Single-key shortcuts" checked={shortcuts} onChange={setShortcuts}
        hint="Keys like J, K, I and A act on the run timeline when focus is not in a text field. Turn off if you use speech input or they get in the way." />
      <p className="text-sm text-fg-muted leading-6">
        <Kbd>⌘K</Kbd> or <Kbd>Ctrl K</Kbd> opens the command palette and always works. Press <Kbd>?</Kbd> in a run to see every shortcut.
      </p>
    </SettingsSection>
  );
}

function ProfileSkeleton() {
  return (
    <div className="rounded-lg border border-border bg-surface p-5 flex flex-col gap-4" aria-hidden>
      <Skeleton className="h-6 w-32" />
      <div className="grid gap-4 sm:grid-cols-2"><Skeleton className="h-14" /><Skeleton className="h-14" /></div>
    </div>
  );
}

export default function Profile() {
  const me = useMe();
  return (
    <SettingsLayout form title="Profile" description="Your account, password, two-factor authentication and how Argus looks on this device.">
      {me.data ? (
        <AccountSection key={me.data.id} user={me.data} />
      ) : me.error ? (
        <div className="rounded-lg border border-border bg-surface"><ErrorState error={me.error} onRetry={() => void me.refetch()} /></div>
      ) : (
        <div role="status" aria-label="Loading profile"><ProfileSkeleton /></div>
      )}
      <PasswordSection />
      {me.data && <MfaSection user={me.data} />}
      <AppearanceSection />
      <KeyboardSection />
    </SettingsLayout>
  );
}
