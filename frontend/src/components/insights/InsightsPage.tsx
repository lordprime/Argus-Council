import { LayoutDashboard, ScrollText } from "lucide-react";
import type { ReactNode } from "react";

import { Page, PageHeader, SectionNav, type SectionLink } from "@/components/ui";

const TABS: SectionLink[] = [
  { to: "/insights", label: "Overview", icon: LayoutDashboard },
  { to: "/insights/audit", label: "Audit log", icon: ScrollText },
];

/**
 * Page frame for the Insights section (C-10): eyebrow, the page's single h1 with meta badges and
 * actions, the section tabs (Overview, Audit log), then the content, at the `wide` width.
 */
export function InsightsPage({ title, description, meta, actions, children }: {
  title: string;
  description?: ReactNode;
  meta?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
}) {
  return (
    <Page width="wide">
      <PageHeader eyebrow="Insights" title={title} description={description} meta={meta} actions={actions} />
      <SectionNav label="Insights" links={TABS} />
      <div className="flex flex-col gap-4 min-w-0">{children}</div>
    </Page>
  );
}
