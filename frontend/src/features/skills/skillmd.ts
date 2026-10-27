// Client-side SKILL.md helpers. The server (POST /skills/validate) is authoritative; these give instant feedback
// and compose a frontmatter block from the editor's structured fields (backend/argus/skills/package.py).

export const NAME_RE = /^[a-z0-9][a-z0-9-]{0,63}$/;
export const MAX_DESCRIPTION = 1024;
export const TEXT_SUFFIXES = [
  ".md", ".txt", ".py", ".js", ".ts", ".json", ".yaml", ".yml", ".toml", ".tex", ".bib", ".csv", ".html", ".css",
  ".sh", ".sql", ".cfg", ".ini", ".rst", ".jinja", ".j2", ".xml", ".svg",
];
export const KNOWN_TOOLS = [
  "web_search", "web_fetch", "notes_read", "notes_write", "artifact_read", "artifact_write", "artifact_list",
  "fs_read", "fs_write", "fs_edit", "fs_list", "fs_search", "fs_delete", "shell", "remote_agent", "use_skill", "skill_read",
];

export interface SkillFields {
  name: string;
  description: string;
  version: string;
  allowedTools: string[];
  body: string;
}

const FRONTMATTER = /^﻿?---\s*\n[\s\S]*?\n---\s*(?:\n|$)/;

/** The instructions after the YAML frontmatter. */
export function stripFrontmatter(md: string): string {
  return md.replace(FRONTMATTER, "").trim();
}

/** JSON strings are valid YAML double-quoted scalars, so quoting through JSON is safe for any user text. */
const q = (s: string) => JSON.stringify(s);

export function composeSkillMd(f: SkillFields): string {
  const lines = ["---", `name: ${q(f.name.trim())}`, `description: ${q(f.description.trim())}`];
  if (f.version.trim()) lines.push(`version: ${q(f.version.trim())}`);
  if (f.allowedTools.length) lines.push(`allowed-tools: [${f.allowedTools.map(q).join(", ")}]`);
  lines.push("---", "", f.body.trim(), "");
  return lines.join("\n");
}

export function parseToolList(s: string): string[] {
  return [...new Set(s.split(/[,\s]+/).map((t) => t.trim()).filter(Boolean))];
}

/** Returns a message for an unsafe/unsupported bundled file path, mirroring `_safe_relpath` + suffix checks. */
export function filePathError(path: string): string | undefined {
  const p = path.trim().replace(/\\/g, "/");
  if (!p) return "Path is required";
  const parts = p.split("/").filter(Boolean);
  if (p.startsWith("/") || parts.includes("..") || parts.some((x) => x.startsWith("."))) return "Use a relative path without '..' or hidden parts";
  if (p === "SKILL.md") return "SKILL.md is generated from the fields above";
  const dot = p.lastIndexOf(".");
  if (dot < 0 || !TEXT_SUFFIXES.includes(p.slice(dot).toLowerCase())) return "Only text files are allowed (.md, .py, .json, …)";
  return undefined;
}

export function fieldErrors(f: SkillFields): Partial<Record<keyof SkillFields, string>> {
  const e: Partial<Record<keyof SkillFields, string>> = {};
  if (!NAME_RE.test(f.name.trim())) e.name = "1-64 characters: lowercase letters, digits and hyphens, starting with a letter or digit";
  if (!f.description.trim()) e.description = "Required. It tells agents when to use the skill";
  else if (f.description.trim().length > MAX_DESCRIPTION) e.description = `At most ${MAX_DESCRIPTION} characters`;
  if (!f.body.trim()) e.body = "Write the instructions agents follow when they use this skill";
  return e;
}

/**
 * Shift Markdown ATX headings down so a SKILL.md "# Title" renders below the page's own h1/h2 (ui-spec §8.10:
 * one h1 per page, correct hierarchy). Fenced code blocks are left untouched.
 */
export function demoteHeadings(md: string, by = 2): string {
  let fence: string | null = null;
  return md
    .split("\n")
    .map((line) => {
      const f = /^\s{0,3}(```+|~~~+)/.exec(line);
      if (f) {
        const mark = f[1]!.slice(0, 3);
        if (fence === null) fence = mark;
        else if (mark === fence) fence = null;
        return line;
      }
      if (fence !== null) return line;
      const h = /^(\s{0,3})(#{1,6})(\s|$)/.exec(line);
      if (!h) return line;
      const level = Math.min(6, h[2]!.length + by);
      return h[1] + "#".repeat(level) + line.slice(h[1]!.length + h[2]!.length);
    })
    .join("\n");
}

/** prose-argus styles h1-h3 only; demoted SKILL.md headings land on h3-h6, so give h4-h6 a visible hierarchy too. */
export const SKILL_PROSE =
  "text-base [&_h3:first-child]:mt-0 [&_h4:first-child]:mt-1 [&_h4]:text-base [&_h4]:font-semibold [&_h4]:mt-5 [&_h4]:mb-1.5 " +
  "[&_h5]:text-sm [&_h5]:font-semibold [&_h5]:mt-4 [&_h5]:mb-1 [&_h6]:text-sm [&_h6]:font-medium [&_h6]:text-fg-muted [&_h6]:mt-3";

export interface FrontmatterCheck {
  field: "name" | "description" | "version" | "allowed-tools" | "body";
  status: "ok" | "warn" | "error";
  message: string;
}

const SEMVER = /^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$/;

/** Live, per-field feedback for the editor. Errors mirror the server's rules; warnings are advice only. */
export function frontmatterChecks(f: SkillFields): FrontmatterCheck[] {
  const errs = fieldErrors(f);
  const desc = f.description.trim();
  const unknown = f.allowedTools.filter((t) => !KNOWN_TOOLS.includes(t));
  const checks: FrontmatterCheck[] = [
    { field: "name", status: errs.name ? "error" : "ok", message: errs.name ?? `“${f.name.trim()}” is a valid skill name` },
    errs.description
      ? { field: "description", status: "error", message: errs.description }
      : !/\buse (it )?(when|for|before|after)\b/i.test(desc)
        ? { field: "description", status: "warn", message: "Consider saying when agents should use it (e.g. “Use when …”)" }
        : { field: "description", status: "ok", message: `${desc.length}/${MAX_DESCRIPTION} characters, says when to use it` },
    !f.version.trim()
      ? { field: "version", status: "warn", message: "No version: the server will default to 1.0.0" }
      : SEMVER.test(f.version.trim())
        ? { field: "version", status: "ok", message: `Version ${f.version.trim()}` }
        : { field: "version", status: "warn", message: "Use semantic versioning, e.g. 1.2.0" },
    !f.allowedTools.length
      ? { field: "allowed-tools", status: "warn", message: "No tools declared: agents get no hint about what the skill needs" }
      : unknown.length
        ? { field: "allowed-tools", status: "warn", message: `Not built-in: ${unknown.join(", ")} (fine for MCP tools)` }
        : { field: "allowed-tools", status: "ok", message: `${f.allowedTools.length} built-in ${f.allowedTools.length === 1 ? "tool" : "tools"}` },
    { field: "body", status: errs.body ? "error" : "ok", message: errs.body ?? "Instructions present" },
  ];
  return checks;
}
