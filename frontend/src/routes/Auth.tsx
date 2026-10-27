import { useState, type FormEvent } from "react";
import { Link, Navigate, useNavigate, useSearchParams } from "react-router";

import { Button, Callout, Card, EmptyState, TextField } from "@/components/ui";
import { api, ApiError, errorMessage, V1 } from "@/lib/api";
import { useSession } from "@/stores/session";

function AuthCard({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="min-h-screen flex items-center justify-center p-4">
      <Card className="w-full max-w-[400px] p-6">
        <div className="flex items-center gap-2 mb-6">
          <img src="/favicon.svg" alt="" className="size-8" />
          <span className="text-lg">Argus Council</span>
        </div>
        <h1 className="text-xl mb-4">{title}</h1>
        {children}
      </Card>
    </div>
  );
}

export function LoginPage() {
  const { login, status } = useSession();
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const next = params.get("next");
  const target = next && next.startsWith("/") && !next.startsWith("//") ? next : "/";
  if (status === "authenticated") return <Navigate to={target} replace />;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await login(email, password);
      navigate(target, { replace: true });
    } catch (err) {
      setError(err instanceof ApiError && err.code === "rate_limited" ? "Too many attempts. Wait a minute and try again." : errorMessage(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <AuthCard title="Sign in">
      <form onSubmit={submit} className="flex flex-col gap-4" noValidate>
        {error && <Callout tone="danger" title="Could not sign in">{error}</Callout>}
        <TextField label="Email" type="email" autoComplete="email" required value={email} onChange={(e) => setEmail(e.target.value)} autoFocus />
        <TextField label="Password" type="password" autoComplete="current-password" required value={password} onChange={(e) => setPassword(e.target.value)} />
        <Button type="submit" variant="primary" loading={busy}>Sign in</Button>
        <p className="text-sm text-fg-muted">No account? <Link className="text-accent underline" to="/register">Create one</Link></p>
      </form>
    </AuthCard>
  );
}

export function RegisterPage() {
  const { login } = useSession();
  const navigate = useNavigate();
  const [form, setForm] = useState({ email: "", password: "", display_name: "" });
  const [error, setError] = useState<string | null>(null);
  const [closed, setClosed] = useState(false);
  const [busy, setBusy] = useState(false);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.post(`${V1}/auth/register`, form);
      await login(form.email, form.password);
      navigate("/", { replace: true });
    } catch (err) {
      if (err instanceof ApiError && err.status === 403) setClosed(true);
      else setError(errorMessage(err));
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
      <form onSubmit={submit} className="flex flex-col gap-4" noValidate>
        <p className="text-sm text-fg-muted">The first account on a new deployment becomes the administrator.</p>
        {error && <Callout tone="danger" title="Could not register">{error}</Callout>}
        <TextField label="Name" autoComplete="name" value={form.display_name} onChange={(e) => setForm({ ...form, display_name: e.target.value })} />
        <TextField label="Email" type="email" autoComplete="email" required value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} />
        <TextField label="Password" type="password" autoComplete="new-password" required minLength={12} hint="At least 12 characters"
          value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} />
        <Button type="submit" variant="primary" loading={busy}>Create account</Button>
        <p className="text-sm text-fg-muted">Have an account? <Link className="text-accent underline" to="/login">Sign in</Link></p>
      </form>
    </AuthCard>
  );
}
