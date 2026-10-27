import { Badge, Callout, SwitchTrack } from "@/components/ui";
import { Chips } from "@/components/connections/parts";
import { effectiveToolRisk, toolAllowed, type ConnectionTestResult, type McpServer, type McpTrust } from "@/features/connections/queries";
import { cn } from "@/lib/cn";

type Tool = McpServer["tools"][number];

export interface ToolSettings {
  tool_allow: string[];
  require_approval: string[];
  risk: string[];
  trust: McpTrust;
}

const WILDCARD = /[*?[]/;

/**
 * New tool_allow after switching one tool on or off. Turning a tool off that a wildcard covers expands the wildcard into
 * the other known tools it covered, so the rest stay allowed.
 */
export function nextToolAllow(list: string[], known: string[], tool: string, on: boolean): string[] {
  if (on) return toolAllowed(tool, list) ? list : [...list, tool];
  const literals = list.filter((p) => !WILDCARD.test(p) && p !== tool);
  const covered = known.filter((n) => n !== tool && toolAllowed(n, list));
  return [...new Set([...literals, ...covered])];
}

/**
 * Discovered tools with their effective settings. Editable (dialog): an "Allowed" checkbox (tool_allow) and an
 * "Always ask" switch (require_approval) per tool. Read-only (card): the same columns as badges.
 * Narrow screens fold the description under the tool name; the region scrolls rather than the page.
 */
export function ToolTable({ server, tools, settings, onChange }: {
  server: string;
  tools: Tool[];
  settings: ToolSettings;
  onChange?: (patch: Partial<Pick<ToolSettings, "tool_allow" | "require_approval">>) => void;
}) {
  const known = tools.map((t) => t.name);
  const blocked = settings.trust === "blocked";
  return (
    <div tabIndex={0} role="region" aria-label={`Tools from ${server}`} className="relative max-h-72 overflow-auto rounded-md border border-border">
      <table className="w-full text-sm">
        <caption className="sr-only">
          Tools discovered on {server}. {blocked ? "The server is blocked, so none are attached to runs." : ""}
        </caption>
        <thead className="bg-surface-2 text-left text-xs text-fg-subtle sticky top-0 z-[1]">
          <tr>
            <th scope="col" className="px-2 sm:px-3 py-1.5 font-medium">Tool</th>
            <th scope="col" className="hidden sm:table-cell px-3 py-1.5 font-medium">Description</th>
            <th scope="col" className="px-2 sm:px-3 py-1.5 font-medium">Allowed</th>
            <th scope="col" className="px-2 sm:px-3 py-1.5 font-medium">Always ask</th>
            <th scope="col" className="px-2 sm:px-3 py-1.5 font-medium">Risk tags</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-border">
          {tools.map((t) => {
            const allowed = toolAllowed(t.name, settings.tool_allow);
            const ask = settings.require_approval.includes(t.name);
            const risk = effectiveToolRisk(settings, t.name);
            return (
              <tr key={t.name} className={cn(!allowed && "text-fg-subtle")}>
                <td className="px-2 sm:px-3 py-1.5 align-top min-w-0">
                  <span className="font-mono text-xs break-all">{t.name}</span>
                  {t.description && <span aria-hidden className="sm:hidden block text-xs text-fg-muted break-words">{t.description}</span>}
                </td>
                <td className="hidden sm:table-cell px-3 py-1.5 text-fg-muted align-top break-words">{t.description || "—"}</td>
                <td className="px-2 sm:px-3 py-1.5 align-top">
                  {onChange ? (
                    <input type="checkbox" checked={allowed} aria-label={`Allow ${t.name}`}
                      onChange={(e) => onChange({ tool_allow: nextToolAllow(settings.tool_allow, known, t.name, e.target.checked) })}
                      className="size-4 mt-0.5 accent-[rgb(var(--accent))]" />
                  ) : allowed ? <Badge tone="success">Yes</Badge> : <Badge>No</Badge>}
                </td>
                <td className="px-2 sm:px-3 py-1.5 align-top">
                  {onChange ? (
                    <button type="button" role="switch" aria-checked={ask} aria-label={`Always ask before ${t.name}`}
                      onClick={() => onChange({ require_approval: ask ? settings.require_approval.filter((x) => x !== t.name) : [...settings.require_approval, t.name] })}
                      className="mt-0.5 inline-flex rounded-full">
                      <SwitchTrack checked={ask} />
                    </button>
                  ) : ask ? <Badge tone="warning">Yes</Badge> : <span className="text-fg-subtle">—</span>}
                </td>
                <td className="px-2 sm:px-3 py-1.5 align-top">
                  <Chips items={risk} label={`Risk tags for ${t.name}`} />
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

/** Result of a connection test (discovery): tool count, latency and server name, or the error. */
export function TestSummary({ result }: { result: ConnectionTestResult }) {
  const n = result.tools.length;
  return result.ok ? (
    <Callout tone="success" title={`Connected · ${n} ${n === 1 ? "tool" : "tools"} · ${result.latencyMs} ms`}>
      {result.serverName ? `Server reports ${result.serverName}.` : "Tools were listed successfully."}
      {n === 0 && " No tools passed the allowed-tools filter."}
    </Callout>
  ) : (
    <Callout tone="danger" title={`Connection failed · ${result.latencyMs} ms`}>{result.error}</Callout>
  );
}
