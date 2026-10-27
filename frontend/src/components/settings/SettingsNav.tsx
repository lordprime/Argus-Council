import { Database, KeyRound, UserRound, Users, Webhook } from "lucide-react";
import type { ReactNode } from "react";

import { Page, PageHeader, SectionNav, type SectionLink } from "@/components/ui";
import { useIsAdmin } from "@/features/admin/queries";
import { cn } from "@/lib/cn";

const LINKS: (SectionLink & { adminOnly?: boolean })[] = [
  { to: "/settings/profile", label: "Profile", icon: UserRound },
  { to: "/settings/api-keys", label: "API keys", icon: KeyRound },
  { to: "/settings/webhooks", label: "Webhooks", icon: Webhook },
  { to: "/settings/data", label: "Data & privacy", icon: Database },
  { to: "/settings/users", label: "Users", icon: Users, adminOnly: true },
];

/** Sub-navigation shared by the settings pages. NavLink sets aria-current="page" on the active link. */
export function SettingsNav() {
  const admin = useIsAdmin();
  return <SectionNav label="Settings" links={LINKS.filter((l) => !l.adminOnly || admin)} />;
}

/**
 * Page frame for /settings/*, in the shared section order (C-10): eyebrow, the page's single h1, the
 * section tabs, then sections. Every settings page uses the `default` width so switching tabs never
 * moves the header or the tabs. Single-column form pages (Profile, Data) pass `form` to cap the content
 * column at the form measure (880px) under the same, unmoved header; tables use the full width.
 */
export function SettingsLayout({ title, description, actions, form, children }: {
  title: string;
  description?: ReactNode;
  actions?: ReactNode;
  form?: boolean;
  children: ReactNode;
}) {
  return (
    <Page>
      <PageHeader eyebrow="Settings" title={title} description={description} actions={actions} />
      <SettingsNav />
      <div className={cn("flex flex-col gap-6", form && "max-w-[880px]")}>{children}</div>
    </Page>
  );
}

/** A titled settings card. The heading is an h2 so each page keeps a single h1. */
export function SettingsSection({ id, title, description, badge, children, footer }: {
  id: string;
  title: string;
  description?: ReactNode;
  badge?: ReactNode;
  children: ReactNode;
  footer?: ReactNode;
}) {
  return (
    <section aria-labelledby={`${id}-h`} className="rounded-lg border border-border bg-surface shadow-1">
      <div className="px-4 pt-4 md:px-5 md:pt-5">
        <div className="flex items-center gap-2">
          <h2 id={`${id}-h`} className="text-lg">{title}</h2>
          {badge}
        </div>
        {description && <p className="text-sm text-fg-muted mt-0.5">{description}</p>}
      </div>
      <div className="p-4 md:p-5 flex flex-col gap-4">{children}</div>
      {footer && (
        <div className="flex flex-wrap items-center justify-end gap-2 border-t border-border bg-surface-2/50 px-4 py-3 md:px-5 rounded-b-lg">
          {footer}
        </div>
      )}
    </section>
  );
}
