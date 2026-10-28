// Sign-in and registration validation (audit C-06): the same rules the backend enforces
// (api/schemas.py RegisterIn: EmailStr, password 12-256 characters), checked on the client first, and
// server validation errors mapped back onto the fields they belong to in plain words.

import { ApiError } from "@/lib/api";

export type AuthField = "email" | "password" | "display_name";
export type AuthErrors = Partial<Record<AuthField | "_form", string>>;

export const MIN_PASSWORD = 12;
export const MAX_PASSWORD = 256;

// Deliberately loose: one @, something on each side, a dot in the domain. The server has the final say.
const EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

export function validateEmail(email: string): string | undefined {
  const v = email.trim();
  if (!v) return "Enter your email address";
  if (!EMAIL.test(v)) return "Enter a valid email address, like name@example.com";
  return undefined;
}

export function validateLogin(f: { email: string; password: string }): AuthErrors {
  const e: AuthErrors = {};
  const email = validateEmail(f.email);
  if (email) e.email = email;
  if (!f.password) e.password = "Enter your password";
  return e;
}

export function validateRegister(f: { email: string; password: string; display_name: string }): AuthErrors {
  const e: AuthErrors = {};
  const email = validateEmail(f.email);
  if (email) e.email = email;
  if (!f.password) e.password = "Choose a password";
  else if (f.password.length < MIN_PASSWORD) e.password = `Use at least ${MIN_PASSWORD} characters (${f.password.length} so far)`;
  else if (f.password.length > MAX_PASSWORD) e.password = `Use at most ${MAX_PASSWORD} characters`;
  if (f.display_name.length > 120) e.display_name = "Use at most 120 characters";
  return e;
}

const FIELDS: readonly AuthField[] = ["email", "password", "display_name"];
const isField = (k: string): k is AuthField => (FIELDS as readonly string[]).includes(k);

/** Rewrites pydantic's wording ("value is not a valid email address: …") into the app's plain voice. */
function humanize(field: AuthField | "_form", msg: string): string {
  const m = msg.replace(/^Value error, /, "").trim();
  if (field === "email" && /email/i.test(m)) return "Enter a valid email address, like name@example.com";
  const min = /at least (\d+) characters?/i.exec(m);
  if (min) return `Use at least ${min[1]} characters`;
  const max = /at most (\d+) characters?/i.exec(m);
  if (max) return `Use at most ${max[1]} characters`;
  if (/field required|missing/i.test(m)) return field === "password" ? "Enter a password" : field === "email" ? "Enter your email address" : "Required";
  return m.charAt(0).toUpperCase() + m.slice(1);
}

/**
 * Field errors from a failed auth request. Reads `details.errors` (FastAPI's `loc`/`msg` list, as
 * lib/api.ts wraps it), `details.fields` ({field: message}) or, failing both, a "field: message; …"
 * string. Anything that is not about a field lands on `_form`.
 */
export function authErrorsFrom(err: unknown): AuthErrors {
  if (!(err instanceof ApiError)) return { _form: err instanceof Error ? err.message : "Something went wrong" };
  const out: AuthErrors = {};
  const put = (key: string | undefined, msg: string) => {
    const k = key && isField(key) ? key : "_form";
    out[k] ??= humanize(k, msg);
  };
  const list = (err.details as { errors?: { loc?: unknown[]; msg?: string }[] }).errors;
  const fields = (err.details as { fields?: Record<string, unknown> }).fields;
  if (Array.isArray(list) && list.length) {
    for (const e of list) put((e.loc ?? []).filter((x) => x !== "body").map(String)[0], e.msg ?? "Invalid value");
  } else if (fields && typeof fields === "object") {
    for (const [k, v] of Object.entries(fields)) put(k, String(v));
  } else if (err.status === 422 || err.code === "validation_error") {
    for (const part of err.message.split(/;\s*/)) {
      const m = /^([a-z_]+)(?:\.[\w.]+)?:\s*(.+)$/i.exec(part);
      if (m) put(m[1], m[2]!);
      else if (part.trim()) put(undefined, part);
    }
  } else if (err.status === 409) {
    out.email = "An account with this email already exists. Sign in instead.";
  } else {
    out._form = err.status === 401 ? "Email or password is incorrect." : err.message;
  }
  return out;
}
