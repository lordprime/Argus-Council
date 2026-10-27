import * as Dropdown from "@radix-ui/react-dropdown-menu";
import { Command } from "cmdk";
import {
  Activity,
  BarChart3,
  Ellipsis,
  Home,
  KeyRound,
  LayoutTemplate,
  LayoutGrid,
  Library,
  LogOut,
  Monitor,
  Moon,
  Plug,
  Plus,
  ScrollText,
  Settings,
  Sparkles,
  Sun,
  Users,
  PanelLeftClose,
  PanelLeftOpen,
  Search,
  type LucideIcon,
} from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import { Link, Outlet, useLocation, useNavigate } from "react-router";

import { Breadcrumbs } from "@/components/layout/Breadcrumbs";
import { NotificationBell } from "@/components/notifications/NotificationBell";
import { IconButton, Kbd, Toaster, touchSquare } from "@/components/ui";
import { Drawer } from "@/components/ui/Drawer";
import { cn } from "@/lib/cn";
import { useRunsList } from "@/lib/queries";
import { BREAKPOINT, useMediaQuery } from "@/lib/useMediaQuery";
import { useSession } from "@/stores/session";
import { useUi, type Theme } from "@/stores/ui";

interface NavItem {
  to: string;
  label: string;
  icon: LucideIcon;
  /** Match the path exactly (Home). */
  end?: boolean;
  /** Path prefix that marks the item active when it differs from `to`. */
  match?: string;
}

const NAV: NavItem[] = [
  { to: "/", label: "Home", icon: Home, end: true },
  { to: "/runs", label: "Runs", icon: Activity },
  { to: "/councils", label: "Councils", icon: Users },
  { to: "/skills", label: "Skills", icon: Sparkles },
  { to: "/knowledge", label: "Knowledge", icon: Library },
  { to: "/templates", label: "Templates", icon: LayoutTemplate },
  { to: "/connections/providers", label: "Connections", icon: Plug, match: "/connections" },
  { to: "/insights", label: "Insights", icon: BarChart3, match: "/insights" },
  { to: "/settings/profile", label: "Settings", icon: Settings, match: "/settings" },
];
/** Mobile bottom tab bar (spec §2): the first three sections, then "More" for the rest. */
const TABS = NAV.slice(0, 3);
const MORE = NAV.slice(3);

export function isActive(n: NavItem, pathname: string): boolean {
  const base = n.match ?? n.to;
  if (n.end) return pathname === base;
  return pathname === base || pathname.startsWith(`${base}/`);
}

const THEMES: { v: Theme; label: string; icon: LucideIcon }[] = [
  { v: "system", label: "System", icon: Monitor },
  { v: "light", label: "Light", icon: Sun },
  { v: "dark", label: "Dark", icon: Moon },
];

const menuItem = cn("flex items-center gap-2 rounded-md px-2 h-8 text-sm outline-none data-[highlighted]:bg-surface-2 cursor-pointer",
  "[@media(pointer:coarse)]:h-11");
const menuPanel = "z-popover min-w-52 rounded-lg border border-border bg-surface shadow-2 p-1";

function ThemeItems() {
  const { theme, setTheme } = useUi();
  return (
    <Dropdown.RadioGroup value={theme} onValueChange={(v) => setTheme(v as Theme)}>
      {THEMES.map((o) => (
        <Dropdown.RadioItem key={o.v} value={o.v} className={cn(menuItem, "data-[state=checked]:font-semibold")}>
          <o.icon className="size-4" aria-hidden /> {o.label}
        </Dropdown.RadioItem>
      ))}
    </Dropdown.RadioGroup>
  );
}

/** Desktop theme switcher. Below md the same choices live in the account menu (audit C-30). */
function ThemeMenu() {
  const { theme } = useUi();
  const current = THEMES.find((o) => o.v === theme) ?? THEMES[0]!;
  return (
    <Dropdown.Root>
      <Dropdown.Trigger asChild>
        <IconButton label={`Theme: ${current.label}`} icon={current.icon} />
      </Dropdown.Trigger>
      <Dropdown.Portal>
        <Dropdown.Content align="end" sideOffset={6} className={cn(menuPanel, "min-w-36")}>
          <ThemeItems />
        </Dropdown.Content>
      </Dropdown.Portal>
    </Dropdown.Root>
  );
}

function UserMenu({ withTheme }: { withTheme: boolean }) {
  const { user, logout } = useSession();
  const navigate = useNavigate();
  const initials = (user?.display_name || user?.email || "?").slice(0, 2).toUpperCase();
  return (
    <Dropdown.Root>
      <Dropdown.Trigger asChild>
        {/* The visible initials start the accessible name ("DA, account menu"), so speech users can say
            what they see (2.5.3 label in name). */}
        <button type="button"
          className={cn("inline-flex items-center justify-center size-8 rounded-full bg-accent-subtle text-accent text-xs font-semibold",
            touchSquare)}>
          {initials}<span className="sr-only">, account menu</span>
        </button>
      </Dropdown.Trigger>
      <Dropdown.Portal>
        <Dropdown.Content align="end" sideOffset={6} className={menuPanel}>
          <div className="px-2 py-1.5 text-xs text-fg-subtle truncate">{user?.email}</div>
          {[
            { label: "Profile", to: "/settings/profile", icon: Settings },
            { label: "API keys", to: "/settings/api-keys", icon: KeyRound },
            { label: "Protocol ledger", to: "/connections/ledger", icon: ScrollText },
          ].map((i) => (
            <Dropdown.Item key={i.to} onSelect={() => navigate(i.to)} className={menuItem}>
              <i.icon className="size-4" aria-hidden /> {i.label}
            </Dropdown.Item>
          ))}
          {withTheme && (
            <>
              <Dropdown.Separator className="my-1 h-px bg-border" />
              <Dropdown.Label className="px-2 py-1 text-2xs uppercase tracking-wide text-fg-subtle">Theme</Dropdown.Label>
              <ThemeItems />
            </>
          )}
          <Dropdown.Separator className="my-1 h-px bg-border" />
          <Dropdown.Item onSelect={() => { logout(); navigate("/login"); }} className={cn(menuItem, "text-danger")}>
            <LogOut className="size-4" aria-hidden /> Sign out
          </Dropdown.Item>
        </Dropdown.Content>
      </Dropdown.Portal>
    </Dropdown.Root>
  );
}

export function CommandPalette() {
  const [open, setOpen] = useState(false);
  const navigate = useNavigate();
  const { setTheme } = useUi();
  const runs = useRunsList({ limit: 20 });
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setOpen((o) => !o);
      }
    };
    window.addEventListener("keydown", onKey);
    const onOpen = () => setOpen(true);
    window.addEventListener("argus:palette", onOpen);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("argus:palette", onOpen);
    };
  }, []);
  const go = (to: string) => {
    setOpen(false);
    navigate(to);
  };
  const item = "flex items-center gap-2 rounded-md px-2 h-9 text-sm cursor-pointer data-[selected=true]:bg-surface-2 [@media(pointer:coarse)]:h-11";
  return (
    <Command.Dialog open={open} onOpenChange={setOpen} label="Command palette"
      overlayClassName="fixed inset-0 bg-black/40 z-palette"
      contentClassName="fixed left-1/2 top-[15vh] -translate-x-1/2 z-palette w-[min(36rem,calc(100vw-2rem))] rounded-xl border border-border bg-surface shadow-3 overflow-hidden">
      <div className="flex items-center gap-2 px-3 border-b border-border">
        <Search className="size-4 text-fg-subtle" aria-hidden />
        <Command.Input placeholder="Search pages, runs, actions…" className="h-11 flex-1 bg-transparent outline-none text-base" />
      </div>
      <Command.List className="max-h-80 overflow-auto p-1">
        <Command.Empty className="p-4 text-sm text-fg-muted">No results.</Command.Empty>
        <Command.Group heading="Navigate" className="text-2xs text-fg-subtle px-1 [&_[cmdk-group-heading]]:px-2 [&_[cmdk-group-heading]]:py-1">
          {[...NAV, { to: "/connections/cli-agents", label: "CLI agents", icon: Plug }, { to: "/connections/mcp", label: "MCP servers", icon: Plug }, { to: "/connections/agents", label: "Remote agents", icon: LayoutGrid },
            { to: "/connections/ledger", label: "Protocol ledger", icon: ScrollText }, { to: "/settings/api-keys", label: "API keys", icon: KeyRound }].map((n) => (
            <Command.Item key={n.to} value={`go ${n.label}`} onSelect={() => go(n.to)} className={item}>
              <n.icon className="size-4" aria-hidden /> {n.label}
            </Command.Item>
          ))}
        </Command.Group>
        {runs.data && runs.data.length > 0 && (
          <Command.Group heading="Recent runs" className="text-2xs text-fg-subtle px-1 [&_[cmdk-group-heading]]:px-2 [&_[cmdk-group-heading]]:py-1">
            {runs.data.slice(0, 8).map((r) => (
              <Command.Item key={r.id} value={`run ${r.topic} ${r.id}`} onSelect={() => go(`/runs/${r.id}`)} className={item}>
                <Activity className="size-4" aria-hidden /> <span className="truncate">{r.topic}</span>
                <span className="ml-auto text-2xs uppercase text-fg-subtle">{r.status}</span>
              </Command.Item>
            ))}
          </Command.Group>
        )}
        <Command.Group heading="Actions" className="text-2xs text-fg-subtle px-1 [&_[cmdk-group-heading]]:px-2 [&_[cmdk-group-heading]]:py-1">
          {/* Plus marks every "New …" action; Sparkles stays reserved for Skills (audit C-31). */}
          <Command.Item value="new run" onSelect={() => go("/")} className={item}><Plus className="size-4" aria-hidden /> New run</Command.Item>
          <Command.Item value="new council" onSelect={() => go("/councils/new")} className={item}><Plus className="size-4" aria-hidden /> New council</Command.Item>
          <Command.Item value="new skill" onSelect={() => go("/skills/new")} className={item}><Plus className="size-4" aria-hidden /> New skill</Command.Item>
          {THEMES.map((t) => (
            <Command.Item key={t.v} value={`theme ${t.v}`} onSelect={() => { setTheme(t.v); setOpen(false); }} className={item}>
              <t.icon className="size-4" aria-hidden /> Theme: {t.label}
            </Command.Item>
          ))}
        </Command.Group>
      </Command.List>
    </Command.Dialog>
  );
}

function ActiveRuns({ collapsed }: { collapsed: boolean }) {
  const active = useRunsList({ status: "running", limit: 8 });
  if (collapsed || !active.data?.length) return null;
  return (
    <div className="mt-6">
      <p className="px-3 text-2xs uppercase tracking-wide text-fg-subtle mb-1">Active runs</p>
      <ul>
        {active.data.map((r) => (
          <li key={r.id}>
            <Link to={`/runs/${r.id}`} className="flex items-center gap-2 px-3 h-8 rounded-md text-sm text-fg-muted hover:bg-surface-2 hover:text-fg">
              <span className="size-2 rounded-full bg-info shrink-0" aria-hidden />
              <span className="truncate">{r.topic}</span>
            </Link>
          </li>
        ))}
      </ul>
    </div>
  );
}

/** Logo mark. The brand asset lives at /favicon.svg, so a new logo drops in without code changes. */
function LogoMark({ className }: { className?: string }) {
  return <img src="/favicon.svg" alt="" className={cn("size-6 shrink-0", className)} />;
}

function SideNav() {
  const { navCollapsed, toggleNav } = useUi();
  const { pathname } = useLocation();
  return (
    <nav aria-label="Primary"
      className={cn("hidden md:flex flex-col border-r border-border bg-surface shrink-0 sticky top-0 h-screen z-rail transition-[width] duration-base",
        navCollapsed ? "w-16" : "w-60")}>
      <div className="flex items-center gap-2 h-12 px-3 border-b border-border">
        <LogoMark />
        {!navCollapsed && <span className="font-semibold">Argus Council</span>}
      </div>
      <ul className="flex flex-col gap-0.5 p-2">
        {NAV.map((n) => {
          const active = isActive(n, pathname);
          return (
            <li key={n.to}>
              <Link to={n.to} aria-current={active ? "page" : undefined} title={navCollapsed ? n.label : undefined}
                aria-label={navCollapsed ? n.label : undefined}
                className={cn("flex items-center gap-3 rounded-md h-9 px-3 text-sm",
                  active ? "bg-accent-subtle text-accent font-medium" : "text-fg-muted hover:bg-surface-2 hover:text-fg")}>
                <n.icon className="size-4 shrink-0" aria-hidden />
                {!navCollapsed && n.label}
              </Link>
            </li>
          );
        })}
      </ul>
      <ActiveRuns collapsed={navCollapsed} />
      <div className="mt-auto p-2">
        <IconButton label={navCollapsed ? "Expand navigation" : "Collapse navigation"} icon={navCollapsed ? PanelLeftOpen : PanelLeftClose} onClick={toggleNav} />
      </div>
    </nav>
  );
}

const tabClass = (active: boolean) =>
  cn("relative flex flex-col items-center justify-center gap-0.5 h-14 text-2xs",
    active ? "text-accent font-semibold" : "text-fg-muted hover:text-fg");
/** Active indicator: a bar on top of the tab, so the state is not carried by colour alone (1.4.1). */
const TabBar = ({ active }: { active: boolean }) =>
  active ? <span aria-hidden className="absolute top-0 inset-x-5 h-0.5 rounded-full bg-accent" /> : null;

/** Mobile bottom tab bar (spec §2, audit C-02): Home, Runs, Councils and a "More" sheet for the rest. */
function TabNav() {
  const { pathname } = useLocation();
  const [more, setMore] = useState(false);
  const moreActive = MORE.some((n) => isActive(n, pathname));
  useEffect(() => setMore(false), [pathname]);
  return (
    <>
      <nav aria-label="Primary mobile"
        className="md:hidden fixed bottom-0 inset-x-0 z-header border-t border-border bg-surface pb-[env(safe-area-inset-bottom)]">
        <ul className="grid grid-cols-4">
          {TABS.map((n) => {
            const active = isActive(n, pathname);
            return (
              <li key={n.to}>
                <Link to={n.to} aria-current={active ? "page" : undefined} className={tabClass(active)}>
                  <TabBar active={active} />
                  <n.icon className="size-5" aria-hidden /> {n.label}
                </Link>
              </li>
            );
          })}
          <li>
            <button type="button" aria-haspopup="dialog" aria-expanded={more} onClick={() => setMore(true)}
              className={cn(tabClass(moreActive), "w-full")}>
              <TabBar active={moreActive} />
              <Ellipsis className="size-5" aria-hidden /> More
              {moreActive && <span className="sr-only">, current section</span>}
            </button>
          </li>
        </ul>
      </nav>
      <Drawer open={more} onOpenChange={setMore} side="bottom" title="More">
        <nav aria-label="More sections">
          <ul className="p-2 flex flex-col gap-0.5">
            {MORE.map((n) => {
              const active = isActive(n, pathname);
              return (
                <li key={n.to}>
                  <Link to={n.to} aria-current={active ? "page" : undefined} onClick={() => setMore(false)}
                    className={cn("flex items-center gap-3 rounded-md h-12 px-3 text-base",
                      active ? "bg-accent-subtle text-accent font-medium" : "text-fg hover:bg-surface-2")}>
                    <n.icon className="size-5 shrink-0" aria-hidden />
                    {n.label}
                  </Link>
                </li>
              );
            })}
          </ul>
        </nav>
      </Drawer>
    </>
  );
}

export function AppShell({ children }: { children?: ReactNode }) {
  const desktop = useMediaQuery(BREAKPOINT.md);
  return (
    <div className="min-h-screen flex">
      <a href="#main" className="sr-only focus:not-sr-only focus:fixed focus:top-2 focus:left-2 focus:z-palette bg-surface px-3 py-2 rounded-md">
        Skip to content
      </a>
      <SideNav />
      <div className="flex-1 min-w-0 flex flex-col">
        <header className="sticky top-0 z-header h-12 flex items-center gap-2 px-3 md:px-4 border-b border-border bg-surface/90 backdrop-blur">
          <Link to="/" className="md:hidden inline-flex items-center justify-center rounded-md size-8 shrink-0">
            <img src="/favicon.svg" alt="Argus Council home" className="size-6" />
          </Link>
          <Breadcrumbs className="flex-1" />
          <button type="button" onClick={() => window.dispatchEvent(new Event("argus:palette"))} aria-keyshortcuts="Meta+K Control+K"
            className={cn("ml-auto shrink-0 flex items-center justify-center md:justify-start gap-2 h-8 w-8 md:w-72 md:px-3 rounded-md",
              "border border-border text-sm text-fg-subtle hover:bg-surface-2", touchSquare)}>
            <Search className="size-4 shrink-0" aria-hidden />
            {/* sr-only (not display:none) on mobile, so the icon-only trigger keeps its name (axe button-name). */}
            <span className="sr-only md:not-sr-only">Search or jump to…</span>
            <span className="hidden md:inline ml-auto"><Kbd>⌘K</Kbd></span>
          </button>
          <div className="flex items-center gap-1 shrink-0">
            <NotificationBell />
            {desktop && <ThemeMenu />}
            <UserMenu withTheme={!desktop} />
          </div>
        </header>
        <main id="main" className="flex-1 min-w-0 pb-tabbar">{children ?? <Outlet />}</main>
      </div>
      <TabNav />
      <CommandPalette />
      <Toaster />
    </div>
  );
}
