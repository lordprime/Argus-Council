import { Bot, Cpu, ScrollText, Server } from "lucide-react";
import type { ReactNode } from "react";

import { Page, PageHeader, SectionNav, type SectionLink } from "@/components/ui";

const TABS: SectionLink[] = [
  { to: "/connections/providers", label: "Providers", icon: Cpu },
  { to: "/connections/mcp", label: "MCP servers", icon: Server },
  { to: "/connections/agents", label: "Remote agents", icon: Bot },
  { to: "/connections/ledger", label: "Ledger", icon: ScrollText },
];

/** Sub-navigation shared by every Connections page: links styled as tabs; the current one has aria-current="page". */
export function ConnectionsNav() {
  return <SectionNav label="Connections" links={TABS} />;
}

/**
 * Page frame for Connections screens, in the shared section order (C-10): eyebrow, the page's single h1
 * with description and actions, the section tabs, then the content.
 */
export function ConnectionsPage({ title, description, actions, children }: {
  title: string;
  description?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
}) {
  return (
    <Page>
      <PageHeader eyebrow="Connections" title={title} description={description} actions={actions} />
      <ConnectionsNav />
      {children}
    </Page>
  );
}
