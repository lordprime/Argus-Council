// Typed fetch client. Access token lives in memory only; the refresh token is kept in
// sessionStorage as an interim measure (see ui-spec §1 "Auth handling").

import type { TokenPair } from "./types";

const REFRESH_KEY = "argus.refresh";

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public details: Record<string, unknown> = {},
  ) {
    super(message);
  }
}

let accessToken: string | null = null;
let refreshing: Promise<boolean> | null = null;
let onAuthLost: (() => void) | null = null;

export const auth = {
  setTokens(t: TokenPair) {
    accessToken = t.access_token;
    try {
      sessionStorage.setItem(REFRESH_KEY, t.refresh_token);
    } catch {
      /* storage disabled: session ends on reload */
    }
  },
  clear() {
    accessToken = null;
    try {
      sessionStorage.removeItem(REFRESH_KEY);
    } catch {
      /* ignore */
    }
  },
  hasSession() {
    try {
      return Boolean(accessToken || sessionStorage.getItem(REFRESH_KEY));
    } catch {
      return Boolean(accessToken);
    }
  },
  get token() {
    return accessToken;
  },
  onLost(cb: () => void) {
    onAuthLost = cb;
  },
};

async function refresh(): Promise<boolean> {
  let stored: string | null = null;
  try {
    stored = sessionStorage.getItem(REFRESH_KEY);
  } catch {
    stored = null;
  }
  if (!stored) return false;
  const resp = await fetch("/api/v1/auth/refresh", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ refresh_token: stored }),
  });
  if (!resp.ok) {
    auth.clear();
    return false;
  }
  auth.setTokens((await resp.json()) as TokenPair);
  return true;
}

/** Single-flight refresh: concurrent 401s share one refresh request. */
export function refreshOnce(): Promise<boolean> {
  refreshing ??= refresh().finally(() => {
    refreshing = null;
  });
  return refreshing;
}

async function toError(resp: Response): Promise<ApiError> {
  let body: unknown = null;
  try {
    body = await resp.json();
  } catch {
    /* non-JSON error */
  }
  const env = (body as { error?: { code?: string; message?: string; details?: Record<string, unknown> } })?.error;
  if (env) return new ApiError(resp.status, env.code ?? "error", env.message ?? resp.statusText, env.details);
  const detail = (body as { detail?: unknown })?.detail;
  if (Array.isArray(detail)) {
    const msg = detail.map((d: { loc?: unknown[]; msg?: string }) => `${(d.loc ?? []).slice(1).join(".")}: ${d.msg}`).join("; ");
    return new ApiError(resp.status, "validation_error", msg, { errors: detail });
  }
  return new ApiError(resp.status, "http_error", typeof detail === "string" ? detail : resp.statusText);
}

export interface RequestOptions extends Omit<RequestInit, "body"> {
  json?: unknown;
  body?: BodyInit;
  raw?: boolean;
}

export async function request<T = unknown>(path: string, opts: RequestOptions = {}, retried = false): Promise<T> {
  if (!accessToken && !retried && auth.hasSession()) await refreshOnce();
  const headers = new Headers(opts.headers);
  if (opts.json !== undefined) headers.set("Content-Type", "application/json");
  if (accessToken) headers.set("Authorization", `Bearer ${accessToken}`);
  const resp = await fetch(path, {
    ...opts,
    headers,
    body: opts.json !== undefined ? JSON.stringify(opts.json) : opts.body,
  });
  if (resp.status === 401 && !retried && !path.startsWith("/api/v1/auth/login")) {
    if (await refreshOnce()) return request<T>(path, opts, true);
    onAuthLost?.();
  }
  if (!resp.ok) throw await toError(resp);
  if (opts.raw) return resp as unknown as T;
  if (resp.status === 204) return undefined as T;
  const ctype = resp.headers.get("content-type") ?? "";
  return (ctype.includes("application/json") ? resp.json() : resp.text()) as Promise<T>;
}

export const api = {
  get: <T>(p: string) => request<T>(p),
  post: <T>(p: string, json?: unknown) => request<T>(p, { method: "POST", json: json ?? {} }),
  put: <T>(p: string, json: unknown) => request<T>(p, { method: "PUT", json }),
  patch: <T>(p: string, json: unknown) => request<T>(p, { method: "PATCH", json }),
  del: <T = void>(p: string) => request<T>(p, { method: "DELETE" }),
};

export const V1 = "/api/v1";

export function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.message;
  if (err instanceof Error) return err.message;
  return "Something went wrong";
}
