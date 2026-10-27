import { Bot, Cpu, ScrollText, Server, type LucideIcon } from "lucide-react";
import type { ReactNode } from "react";
import { NavLink } from "react-router";

import { cn } from "@/lib/cn";

const TABS: { to: string; label: string; icon: LucideIcon }[] = [
  { to: "/connections/providers", label: "Providers", icon: Cpu },
  { to: "/connections/mcp", label: "MCP servers", icon: Server },
  { to: "/connections/agents", label: "Remote agents", icon: Bot },
  { to: "/connections/ledger", label: "Ledger", icon: ScrollText },
];

/** Sub-navigation shared by every Connections page: links styled as tabs; the current one has aria-current="page". */
export function ConnectionsNav() {
  return (
    <nav aria-label="Connections" className="mb-5 -mx-4 md:mx-0 border-b border-border overflow-x-auto">
      <ul className="flex gap-1 px-4 md:px-0 min-w-max">
        {TABS.map((t) => (
          <li key={t.to}>
            <NavLink
              to={t.to}
              className={({ isActive }) =>
                cn(
                  "inline-flex items-center gap-1.5 h-10 px-3 -mb-px text-sm border-b-2 whitespace-nowrap transition-colors duration-fast",
                  isActive ? "border-accent text-fg font-medium" : "border-transparent text-fg-muted hover:text-fg hover:border-border-strong",
                )
              }
            >
              <t.icon className="size-4" aria-hidden />
              {t.label}
            </NavLink>
          </li>
        ))}
      </ul>
    </nav>
  );
}

/** Page frame for Connections screens: sub-nav above the page content, consistent gutters. */
export function ConnectionsPage({ children }: { children: ReactNode }) {
  return (
    <div className="p-4 md:p-6 max-w-6xl mx-auto">
      <ConnectionsNav />
      {children}
    </div>
  );
}
