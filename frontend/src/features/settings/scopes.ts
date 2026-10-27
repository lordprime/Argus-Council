// API key scope presets. The backend checks scopes only on writes ("runs:write", "skills:write"…);
// reads are allowed for every valid key.

export type ScopePreset = "full" | "runs" | "read";

export const SCOPE_PRESETS: { value: ScopePreset; label: string; scopes: string[]; description: string }[] = [
  {
    value: "full",
    label: "Full access",
    scopes: ["*"],
    description: "Everything you can do in the app: start and control runs, edit councils, skills and connections.",
  },
  {
    value: "runs",
    label: "Runs only",
    scopes: ["runs:write"],
    description: "Start, steer and cancel runs, and read results. Cannot change councils, skills or connections.",
  },
  {
    value: "read",
    label: "Read-only",
    // "read" is an explicit, self-describing marker; the backend treats it the same as [] (no writes).
    scopes: ["read"],
    description: "View runs, councils and results. Cannot start runs or change anything.",
  },
];

export function describeScopes(scopes: string[]): string {
  if (scopes.includes("*")) return "Full access";
  const writes = scopes.filter((s) => s.endsWith(":write"));
  if (writes.length === 0) return "Read-only";
  if (writes.length === 1 && writes[0] === "runs:write") return "Runs only";
  return writes.map((s) => s.replace(":write", "")).join(", ");
}
