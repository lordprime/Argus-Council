import { useMemo, useRef, useState, type FormEvent, type ReactNode, type RefObject } from "react";
import { Link, Navigate, useNavigate, useSearchParams } from "react-router";

import { Button, Callout, Card, EmptyState, PasswordField, TextField } from "@/components/ui";
import {
  authErrorsFrom,
  MIN_PASSWORD,
  validateLogin,
  validateRegister,
  type AuthErrors,
  type AuthField,
} from "@/features/auth/validation";
import { api, ApiError, retryAfterSeconds, V1 } from "@/lib/api";
import { useCountdown } from "@/lib/useCountdown";
import { useSession } from "@/stores/session";

function AuthCard({ title, children }: { title: string; children: ReactNode }) {
  return (
    <main id="main" className="min-h-screen flex items-center justify-center p-4">
      <Card className="w-full max-w-[400px] p-6" aria-labelledby="auth-title">
        <div className="flex items-center gap-2 mb-6">
          <img src="/favicon.svg" alt="" className="size-8" />
          <span className="text-lg">Argus Council</span>
        </div>
        <h1 id="auth-title" className="text-xl mb-4">{title}</h1>
        {children}
      </Card>
    </main>
  );
}

const LABELS: Record<AuthField, string> = { email: "Email", password: "Password", display_name: "Name" };

/**
 * Error summary at the top of the form (spec §8.9, as in the council builder): it takes focus on a failed
 * submit and lists each field problem; a rate limit counts down until the next attempt is allowed.
 */
function Summary({ errors, verb, wait, summaryRef }: {
  errors: AuthErrors;
  verb: string;
  wait: number;
  summaryRef: RefObject<HTMLDivElement | null>;
}) {
  const fields = (Object.keys(LABELS) as AuthField[]).filter((k) => errors[k]);
  if (!fields.length && !errors._form && !wait) return null;
  return (
    <div ref={summaryRef} tabIndex={-1} className="focus-visible:outline-none">
      {wait > 0 ? (
        <Callout tone="danger" title="Too many attempts">
          Wait <span className="tabular">{wait}</span> s, then try again.
        </Callout>
      ) : fields.length ? (
        <Callout tone="danger" title={`Fix ${fields.length} problem${fields.length === 1 ? "" : "s"} to ${verb}`}>
          <ul className="list-disc pl-5">
            {fields.map((k) => <li key={k}><span className="font-medium">{LABELS[k]}:</span> {errors[k]}</li>)}
          </ul>
          {errors._form && <p className="mt-1">{errors._form}</p>}
        </Callout>
      ) : (
        <Callout tone="danger" title={`Could not ${verb}`}>{errors._form}</Callout>
      )}
    </div>
  );
}

/** Client errors appear after the first submit and then track the input live; server errors clear on edit. */
function useAuthForm<F extends Record<string, string>>(form: F, validate: (f: F) => AuthErrors) {
  const [submitted, setSubmitted] = useState(false);
  const [server, setServer] = useState<AuthErrors>({});
  const [limited, setLimited] = useState<{ err: unknown; seconds: number | null }>({ err: null, seconds: null });
  const wait = useCountdown(limited.seconds, limited.err);
  const summaryRef = useRef<HTMLDivElement>(null);
  const client = useMemo(() => validate(form), [form, validate]);
  const errors: AuthErrors = { ...(submitted ? client : {}), ...server };
  const focusSummary = () => requestAnimationFrame(() => summaryRef.current?.focus());
  return {
    errors,
    wait,
    summaryRef,
    clear: (k: AuthField) => setServer((e) => {
      if (!e[k] && !e._form) return e;
      const next = { ...e };
      delete next[k];
      delete next._form;
      return next;
    }),
    /** Returns false (and focuses the summary) when the form has client-side problems. */
    begin: () => {
      setSubmitted(true);
      setServer({});
      if (Object.keys(client).length) {
        focusSummary();
        return false;
      }
      return true;
    },
    fail: (err: unknown) => {
      const seconds = retryAfterSeconds(err);
      if (seconds != null) {
        setLimited({ err, seconds });
        setServer({});
      } else {
        setServer(authErrorsFrom(err));
      }
      focusSummary();
    },
  };
}

export function LoginPage() {
  const { login, status } = useSession();
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const [form, setForm] = useState({ email: "", password: "" });
  const [busy, setBusy] = useState(false);
  const f = useAuthForm(form, validateLogin);
  const next = params.get("next");
  const target = next && next.startsWith("/") && !next.startsWith("//") ? next : "/";
  if (status === "authenticated") return <Navigate to={target} replace />;

  const set = (k: "email" | "password", v: string) => {
    setForm((s) => ({ ...s, [k]: v }));
    f.clear(k);
  };
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!f.begin()) return;
    setBusy(true);
    try {
      await login(form.email.trim(), form.password);
      navigate(target, { replace: true });
    } catch (err) {
      f.fail(err);
    } finally {
      setBusy(false);
    }
  };
  return (
    <AuthCard title="Sign in">
      <form onSubmit={submit} className="flex flex-col gap-4" noValidate aria-labelledby="auth-title">
        <Summary errors={f.errors} verb="sign in" wait={f.wait} summaryRef={f.summaryRef} />
        <TextField label="Email" type="email" autoComplete="email" inputMode="email" required autoFocus
          value={form.email} error={f.errors.email} onChange={(e) => set("email", e.target.value)} />
        <PasswordField label="Password" autoComplete="current-password" required
          value={form.password} error={f.errors.password} onChange={(e) => set("password", e.target.value)} />
        <Button type="submit" variant="primary" loading={busy} disabled={f.wait > 0}>Sign in</Button>
        <p className="text-sm text-fg-muted">No account? <Link className="text-accent underline" to="/register">Create one</Link></p>
      </form>
    </AuthCard>
  );
}

export function RegisterPage() {
  const { login } = useSession();
  const navigate = useNavigate();
  const [form, setForm] = useState({ email: "", password: "", display_name: "" });
  const [closed, setClosed] = useState(false);
  const [busy, setBusy] = useState(false);
  const f = useAuthForm(form, validateRegister);
  const set = (k: AuthField, v: string) => {
    setForm((s) => ({ ...s, [k]: v }));
    f.clear(k);
  };
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!f.begin()) return;
    setBusy(true);
    try {
      await api.post(`${V1}/auth/register`, { ...form, email: form.email.trim() });
      await login(form.email.trim(), form.password);
      navigate("/", { replace: true });
    } catch (err) {
      if (err instanceof ApiError && err.status === 403) setClosed(true);
      else f.fail(err);
    } finally {
      setBusy(false);
    }
  };
  if (closed) {
    return (
      <AuthCard title="Registration is closed">
        <EmptyState title="Ask an administrator for an account" body="This Argus deployment does not accept new registrations." action={<Link className="text-accent underline" to="/login">Back to sign in</Link>} />
      </AuthCard>
    );
  }
  return (
    <AuthCard title="Create your account">
      <form onSubmit={submit} className="flex flex-col gap-4" noValidate aria-labelledby="auth-title">
        <p className="text-sm text-fg-muted">The first account on a new deployment becomes the administrator.</p>
        <Summary errors={f.errors} verb="create your account" wait={f.wait} summaryRef={f.summaryRef} />
        <TextField label="Name" autoComplete="name" maxLength={120} value={form.display_name} error={f.errors.display_name}
          onChange={(e) => set("display_name", e.target.value)} />
        <TextField label="Email" type="email" autoComplete="email" inputMode="email" required
          value={form.email} error={f.errors.email} onChange={(e) => set("email", e.target.value)} />
        <PasswordField label="Password" autoComplete="new-password" required minLength={MIN_PASSWORD} hint={`At least ${MIN_PASSWORD} characters`}
          value={form.password} error={f.errors.password} onChange={(e) => set("password", e.target.value)} />
        <Button type="submit" variant="primary" loading={busy} disabled={f.wait > 0}>Create account</Button>
        <p className="text-sm text-fg-muted">Have an account? <Link className="text-accent underline" to="/login">Sign in</Link></p>
      </form>
    </AuthCard>
  );
}
