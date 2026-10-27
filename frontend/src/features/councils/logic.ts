// Pure helpers for the Councils area. Kept free of React so they are easy to unit test.

import type { LucideIcon } from "lucide-react";

import { ApiError } from "@/lib/api";
import { MODE_META } from "@/lib/modes";
import type { Member, Mode, RoleInfo } from "@/lib/types";
import { rosterFor } from "@/routes/Home";

export { rosterFor };

/** Mode picker options, derived from the shared MODE_META so labels, hints and icons match everywhere. */
export const MODES: { value: Mode; label: string; hint: string; icon: LucideIcon }[] = (Object.keys(MODE_META) as Mode[])
  .map((value) => ({ value, ...MODE_META[value] }));
export const MODE_LABEL: Record<string, string> = Object.fromEntries(MODES.map((m) => [m.value, m.label]));
export const DEFAULT_SIZE: Record<Mode, number> = { research: 5, coding: 6, chat: 2, general: 4 };

/** Backend hard limits (schemas.py: ge=1, le=64). */
export const SIZE_FLOOR = 1;
export const SIZE_CEIL = 64;

/** Python's `^[\w .\-]+$` with Unicode word characters. */
export const NAME_RE = /^[\p{L}\p{N}_ .-]+$/u;

export function validateName(name: string): string | undefined {
  const n = name.trim();
  if (!n) return "Name is required.";
  if (n.length > 80) return "Use at most 80 characters.";
  if (!NAME_RE.test(n)) return "Use letters, numbers, spaces, dots, dashes and underscores only.";
  return undefined;
}

export function validateBounds(min: number, max: number): { min_size?: string; max_size?: string } {
  const out: { min_size?: string; max_size?: string } = {};
  if (!Number.isInteger(min) || min < SIZE_FLOOR || min > SIZE_CEIL) out.min_size = `Enter a whole number from ${SIZE_FLOOR} to ${SIZE_CEIL}.`;
  if (!Number.isInteger(max) || max < SIZE_FLOOR || max > SIZE_CEIL) out.max_size = `Enter a whole number from ${SIZE_FLOOR} to ${SIZE_CEIL}.`;
  if (!out.min_size && !out.max_size && min > max) out.max_size = "Maximum must be at least the minimum.";
  return out;
}

export const clamp = (n: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, n));

export function roleTitle(roles: RoleInfo[] | undefined, key: string): string {
  return roles?.find((r) => r.key === key)?.title ?? key.charAt(0).toUpperCase() + key.slice(1);
}

// ------------------------------------------------------------------ resize preview
export interface ResizePreview {
  added: { role: string; name: string }[];
  removed: Member[];
}

/**
 * Mirrors backend services/councils.py `resize_council`: shrinking drops the seats with the
 * highest positions; growing first fills roles the mode's roster wants but the council lacks,
 * then pads with extra workers, naming seats "Title", "Title 2", ...
 */
export function previewResize(
  members: Member[],
  mode: Mode,
  size: number,
  rosters: Record<string, string[]> | undefined,
  overseers: Set<string>,
  roles: RoleInfo[] | undefined,
): ResizePreview {
  const sorted = [...members].sort((a, b) => a.position - b.position);
  const current = sorted.length;
  if (size === current) return { added: [], removed: [] };
  if (size < current) return { added: [], removed: sorted.slice(size) };

  const wanted = rosterFor(rosters, overseers, mode, size);
  const have = countBy(sorted.map((m) => m.role));
  const want = countBy(wanted);
  const deficit = new Map<string, number>();
  for (const [role, n] of want) deficit.set(role, Math.max(0, n - (have.get(role) ?? 0)));
  let additions: string[] = [];
  for (const role of wanted) {
    const d = deficit.get(role) ?? 0;
    if (d > 0) {
      additions.push(role);
      deficit.set(role, d - 1);
    }
  }
  const extra = rosterFor(rosters, overseers, mode, size + current).slice(size);
  additions = [...additions, ...extra].slice(0, size - current);

  const taken = new Map(have);
  const added = additions.map((role) => {
    const n = (taken.get(role) ?? 0) + 1;
    taken.set(role, n);
    const title = roleTitle(roles, role);
    return { role, name: n === 1 ? title : `${title} ${n}` };
  });
  return { added, removed: [] };
}

function countBy(xs: string[]): Map<string, number> {
  const m = new Map<string, number>();
  for (const x of xs) m.set(x, (m.get(x) ?? 0) + 1);
  return m;
}

/** Name for a newly added seat of `role`, matching the backend's "Title N" convention. */
export function nextSeatName(existing: { role: string }[], role: string, roles: RoleInfo[] | undefined): string {
  const n = existing.filter((m) => m.role === role).length + 1;
  const title = roleTitle(roles, role);
  return n === 1 ? title : `${title} ${n}`;
}

// ------------------------------------------------------------------ guardrails
export type Dict = Record<string, unknown>;

/** Backend defaults (runtime/guardrails.py `Guardrails`). The editor shows these when no override is set. */
export const GUARDRAIL_DEFAULTS = {
  budget: { usd: 5, tokens: 2_000_000 },
  max_steps: 80,
  max_runtime_minutes: 60,
  max_parallel_agents: 4,
  tools: { allow: ["*"], deny: [] as string[] },
  approval_required: ["shell", "package_install", "remote_agent", "network_write"],
  content_policy: { preset: "standard", blocked_topics: [] as string[] },
  oversight: { intercept_mode: "hold", monitor_sensitivity: "medium" },
};

const isDict = (v: unknown): v is Dict => typeof v === "object" && v !== null && !Array.isArray(v);

export function getPath(obj: unknown, path: string[]): unknown {
  let cur: unknown = obj;
  for (const k of path) {
    if (!isDict(cur)) return undefined;
    cur = cur[k];
  }
  return cur;
}

/** Effective value: the override when present, else the backend default. */
export function effective(overrides: Dict, path: string[]): unknown {
  const v = getPath(overrides, path);
  return v === undefined ? getPath(GUARDRAIL_DEFAULTS, path) : v;
}

export function deepEqual(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  if (Array.isArray(a) && Array.isArray(b)) return a.length === b.length && a.every((x, i) => deepEqual(x, b[i]));
  if (isDict(a) && isDict(b)) {
    const ka = Object.keys(a);
    return ka.length === Object.keys(b).length && ka.every((k) => deepEqual(a[k], b[k]));
  }
  return false;
}

/**
 * Immutably set `path` in a partial guardrails dict. Setting a value equal to the backend default
 * removes the override (and any parent left empty), so only keys the user changed are sent.
 */
export function setOverride(overrides: Dict, path: string[], value: unknown): Dict {
  const isDefault = deepEqual(value, getPath(GUARDRAIL_DEFAULTS, path));
  const rec = (node: Dict, i: number): Dict => {
    const key = path[i]!;
    const out = { ...node };
    if (i === path.length - 1) {
      if (isDefault) delete out[key];
      else out[key] = value;
    } else {
      const child = rec(isDict(node[key]) ? (node[key] as Dict) : {}, i + 1);
      if (Object.keys(child).length) out[key] = child;
      else delete out[key];
    }
    return out;
  };
  return rec(overrides, 0);
}

/** Count of leaf overrides, for summaries like "3 custom limits". */
export function countOverrides(overrides: Dict): number {
  return Object.values(overrides).reduce<number>((n, v) => n + (isDict(v) ? countOverrides(v) : 1), 0);
}

// ------------------------------------------------------------------ server errors -> field errors
export type FieldErrors = Record<string, string>;

/**
 * Map an ApiError onto form fields. 409 is a duplicate name; FastAPI 422s carry
 * `details.errors[].loc` (["body", field, ...]); domain 422s (guardrail_violation) carry a message
 * we route by keyword. Anything unmatched lands on `_form`.
 */
export function fieldErrorsFrom(err: unknown): FieldErrors {
  if (!(err instanceof ApiError)) return { _form: err instanceof Error ? err.message : "Something went wrong" };
  if (err.status === 409) return { name: err.message };
  const out: FieldErrors = {};
  const list = (err.details as { errors?: { loc?: unknown[]; msg?: string }[] }).errors;
  if (Array.isArray(list)) {
    for (const e of list) {
      const loc = (e.loc ?? []).filter((x) => x !== "body").map(String);
      const msg = (e.msg ?? "Invalid value").replace(/^Value error, /, "");
      const key = loc[0] ?? routeByMessage(msg);
      out[key] ??= capitalize(msg);
    }
    return out;
  }
  return { [routeByMessage(err.message)]: err.message };
}

function routeByMessage(msg: string): string {
  if (/min_size must be/i.test(msg)) return "max_size";
  if (/size|outside \[/i.test(msg)) return "size";
  return "_form";
}

const capitalize = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);
