import { KeyRound, UserRound, Users, Webhook } from "lucide-react";
import type { ReactNode } from "react";
import { NavLink } from "react-router";

import { PageHeader } from "@/components/ui";
import { useIsAdmin } from "@/features/admin/queries";
import { cn } from "@/lib/cn";

const LINKS = [
  { to: "/settings/profile", label: "Profile", icon: UserRound },
  { to: "/settings/api-keys", label: "API keys", icon: KeyRound },
  { to: "/settings/webhooks", label: "Webhooks", icon: Webhook },
  { to: "/settings/users", label: "Users", icon: Users, adminOnly: true },
];

/** Sub-navigation shared by the settings pages. NavLink sets aria-current="page" on the active link. */
export function SettingsNav() {
  const admin = useIsAdmin();
  const links = LINKS.filter((l) => !("adminOnly" in l) || admin);
  return (
    <nav aria-label="Settings" className="border-b border-border mb-6">
      <ul className="flex gap-1 -mb-px overflow-x-auto">
        {links.map((l) => (
          <li key={l.to}>
            <NavLink to={l.to} end
              className={({ isActive }) => cn(
                "inline-flex items-center gap-2 h-10 px-3 text-sm border-b-2 whitespace-nowrap transition-colors duration-fast",
                isActive ? "border-accent text-fg font-medium" : "border-transparent text-fg-muted hover:text-fg hover:border-border-strong",
              )}>
              <l.icon className="size-4" aria-hidden />
              {l.label}
            </NavLink>
          </li>
        ))}
      </ul>
    </nav>
  );
}

/** Page frame for /settings/*: eyebrow, the page's single h1, the sub-nav, then sections. */
export function SettingsLayout({ title, description, actions, children }: {
  title: string;
  description?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="max-w-3xl mx-auto p-4 md:p-6">
      <p className="text-xs font-medium uppercase tracking-wide text-fg-subtle mb-1">Settings</p>
      <PageHeader title={title} description={description} actions={actions} />
      <SettingsNav />
      <div className="flex flex-col gap-6">{children}</div>
    </div>
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
