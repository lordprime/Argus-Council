// Finding the JSON in an agent's reply, so the timeline can show a plan, a debate move or a ballot as a
// readable view instead of a wall of braces. Parsing is JSON.parse only (never eval). A reply that does
// not parse, including one still streaming, stays text.

export type JsonValue = string | number | boolean | null | JsonValue[] | { [k: string]: JsonValue };
export type JsonObject = { [k: string]: JsonValue };

/** The JSON found in a reply, and the prose around it. `raw` is the JSON exactly as the agent wrote it. */
export interface JsonPart {
  before: string;
  after: string;
  raw: string;
  value: JsonObject | JsonValue[];
}

/** Replies larger than this are left as text: parsing them on every streamed delta is not worth it. */
const MAX_CHARS = 200_000;
/** How many top-level `{` candidates an embedded search tries before giving up. */
const MAX_CANDIDATES = 20;

export const isObject = (v: unknown): v is JsonObject => Boolean(v) && typeof v === "object" && !Array.isArray(v);

function parse(src: string): JsonObject | JsonValue[] | undefined {
  try {
    const v = JSON.parse(src) as unknown;
    if (isObject(v) && Object.keys(v).length > 0) return v;
    if (Array.isArray(v) && v.length > 0) return v as JsonValue[];
  } catch {
    /* not JSON (or not yet: still streaming) */
  }
  return undefined;
}

/** Index of the bracket that closes the one at `start`, string-aware; -1 when it never closes. */
function closing(s: string, start: number): number {
  let depth = 0;
  let inString = false;
  let escaped = false;
  for (let i = start; i < s.length; i++) {
    const c = s[i];
    if (inString) {
      if (escaped) escaped = false;
      else if (c === "\\") escaped = true;
      else if (c === '"') inString = false;
      continue;
    }
    if (c === '"') inString = true;
    else if (c === "{" || c === "[") depth++;
    else if (c === "}" || c === "]") {
      depth--;
      if (depth === 0) return i;
      if (depth < 0) return -1;
    }
  }
  return -1;
}

const FENCE = /```[ \t]*(json|jsonc|JSON)?[ \t]*\r?\n([\s\S]*?)\r?\n?[ \t]*```/g;

/**
 * The JSON in a reply: the whole text, a ```json (or bare) fence, or an object embedded in prose.
 * Returns null when there is none, or it does not parse yet.
 */
export function extractJson(text: string): JsonPart | null {
  if (!text || text.length > MAX_CHARS) return null;
  const trimmed = text.trim();
  if (trimmed.startsWith("{") || trimmed.startsWith("[")) {
    const whole = parse(trimmed);
    if (whole) return { before: "", after: "", raw: trimmed, value: whole };
  }
  for (const m of text.matchAll(FENCE)) {
    const inner = (m[2] ?? "").trim();
    if (!inner.startsWith("{") && !inner.startsWith("[")) continue;
    const value = parse(inner);
    if (!value) continue;
    const at = m.index ?? 0;
    return { before: text.slice(0, at).trim(), after: text.slice(at + m[0].length).trim(), raw: inner, value };
  }
  // An object in prose. Only top-level candidates count: a nested object inside one that does not parse
  // (for example a half-streamed plan) must not be shown as if it were the whole answer.
  // Code blocks in other languages are left alone: a dict literal in a Python block is not the answer.
  const code = [...text.matchAll(/```[\s\S]*?(?:```|$)/g)].map((m) => [m.index ?? 0, (m.index ?? 0) + m[0].length] as const);
  let from = 0;
  for (let n = 0; n < MAX_CANDIDATES; n++) {
    const start = text.indexOf("{", from);
    if (start < 0) return null;
    const block = code.find(([a, b]) => start >= a && start < b);
    if (block) {
      from = block[1];
      continue;
    }
    const end = closing(text, start);
    if (end < 0) return null; // unclosed: still streaming, or not JSON
    const raw = text.slice(start, end + 1);
    const value = parse(raw);
    if (value && isObject(value)) {
      return { before: text.slice(0, start).trim(), after: text.slice(end + 1).trim(), raw, value };
    }
    from = end + 1;
  }
  return null;
}

/** What a reply is, by its shape. Anything unrecognised is a generic tree. */
export type JsonKind = "plan" | "challenges" | "responses" | "position" | "cosign" | "amendments" | "vote" | "oversight" | "tree";

const allObjects = (v: unknown): v is JsonObject[] => Array.isArray(v) && v.every(isObject);

export function classifyJson(v: unknown): JsonKind {
  if (!isObject(v)) return "tree";
  if (allObjects(v.steps) && v.steps.length > 0 && v.steps.every((s) => typeof s.title === "string" || typeof s.instructions === "string")) {
    return "plan";
  }
  if (allObjects(v.responses)) return "responses";
  const debateKeys = ["challenges", "resolutions", "claims"].filter((k) => k in v);
  if (debateKeys.length > 0 && debateKeys.every((k) => allObjects(v[k]))) return "challenges";
  if (typeof v.conclusion === "string" || allObjects(v.stances)) return "position";
  if (typeof v.action === "string") {
    const a = v.action.toLowerCase();
    if (a === "pass" || a === "annotate" || a === "hold" || "severity" in v || "correction" in v) return "oversight";
    if (a === "sign" || a === "amend" || a === "block") return "cosign";
  }
  if (Array.isArray(v.endorse)) return "amendments";
  if (typeof v.option === "string" && v.option.trim()) return "vote";
  return "tree";
}

/** The top-level keys each purpose-built view shows; any other key is shown beneath it as a tree. */
export const SHOWN_KEYS: Record<JsonKind, string[]> = {
  plan: ["steps"],
  challenges: ["challenges", "resolutions", "claims"],
  responses: ["responses"],
  position: ["conclusion", "stances", "alternative"],
  cosign: ["action", "reason", "amendment", "claim_id"],
  amendments: ["endorse", "reasons"],
  vote: ["option", "rationale"],
  oversight: ["action", "reason", "severity", "note", "correction"],
  tree: [],
};

/** A scalar as display text ("" for objects, arrays and null). */
export function str(v: unknown): string {
  if (typeof v === "string") return v.trim();
  if (typeof v === "number" || typeof v === "boolean") return String(v);
  return "";
}

/** A string list from a JSON array of strings or numbers (anything else is dropped). */
export function strList(v: unknown): string[] {
  return Array.isArray(v) ? v.map(str).filter(Boolean) : [];
}
