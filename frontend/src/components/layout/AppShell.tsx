import * as Dropdown from "@radix-ui/react-dropdown-menu";
import { Command } from "cmdk";
import {
  Activity,
  BarChart3,
  Home,
  KeyRound,
  LayoutGrid,
  LogOut,
  Monitor,
  Moon,
  Plug,
  ScrollText,
  Settings,
  Sparkles,
  Sun,
  Users,
  PanelLeftClose,
  PanelLeftOpen,
  Search,
} from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import { Link, NavLink, Outlet, useLocation, useNavigate } from "react-router";

import { IconButton, Kbd, Toaster } from "@/components/ui";
import { cn } from "@/lib/cn";
import { useRunsList } from "@/lib/queries";
import { useSession } from "@/stores/session";
import { useUi, type Theme } from "@/stores/ui";

const NAV = [
  { to: "/", label: "Home", icon: Home, end: true },
  { to: "/runs", label: "Runs", icon: Activity },
  { to: "/councils", label: "Councils", icon: Users },
  { to: "/skills", label: "Skills", icon: Sparkles },
  { to: "/connections/providers", label: "Connections", icon: Plug, match: "/connections" },
  { to: "/insights", label: "Insights", icon: BarChart3, match: "/insights" },
  { to: "/settings/profile", label: "Settings", icon: Settings, match: "/settings" },
];

function ThemeMenu() {
  const { theme, setTheme } = useUi();
  const opts: { v: Theme; label: string; icon: typeof Sun }[] = [
    { v: "system", label: "System", icon: Monitor },
    { v: "light", label: "Light", icon: Sun },
    { v: "dark", label: "Dark", icon: Moon },
  ];
  const Current = opts.find((o) => o.v === theme)?.icon ?? Monitor;
  return (
    <Dropdown.Root>
      <Dropdown.Trigger asChild>
        <IconButton label={`Theme: ${theme}`} icon={Current} />
      </Dropdown.Trigger>
      <Dropdown.Portal>
        <Dropdown.Content align="end" sideOffset={6} className="z-popover min-w-36 rounded-lg border border-border bg-surface shadow-2 p-1">
          <Dropdown.RadioGroup value={theme} onValueChange={(v) => setTheme(v as Theme)}>
            {opts.map((o) => (
              <Dropdown.RadioItem key={o.v} value={o.v}
                className="flex items-center gap-2 rounded-md px-2 h-8 text-sm outline-none data-[highlighted]:bg-surface-2 data-[state=checked]:font-semibold cursor-pointer">
                <o.icon className="size-4" aria-hidden /> {o.label}
              </Dropdown.RadioItem>
            ))}
          </Dropdown.RadioGroup>
        </Dropdown.Content>
      </Dropdown.Portal>
    </Dropdown.Root>
  );
}

function UserMenu() {
  const { user, logout } = useSession();
  const navigate = useNavigate();
  const initials = (user?.display_name || user?.email || "?").slice(0, 2).toUpperCase();
  return (
    <Dropdown.Root>
      <Dropdown.Trigger asChild>
        <button aria-label="Account menu" className="size-8 rounded-full bg-accent-subtle text-accent text-xs font-semibold">
          {initials}
        </button>
      </Dropdown.Trigger>
      <Dropdown.Portal>
        <Dropdown.Content align="end" sideOffset={6} className="z-popover min-w-52 rounded-lg border border-border bg-surface shadow-2 p-1">
          <div className="px-2 py-1.5 text-xs text-fg-subtle truncate">{user?.email}</div>
          {[
            { label: "Profile", to: "/settings/profile", icon: Settings },
            { label: "API keys", to: "/settings/api-keys", icon: KeyRound },
            { label: "Protocol ledger", to: "/connections/ledger", icon: ScrollText },
          ].map((i) => (
            <Dropdown.Item key={i.to} onSelect={() => navigate(i.to)}
              className="flex items-center gap-2 rounded-md px-2 h-8 text-sm outline-none data-[highlighted]:bg-surface-2 cursor-pointer">
              <i.icon className="size-4" aria-hidden /> {i.label}
            </Dropdown.Item>
          ))}
          <Dropdown.Separator className="my-1 h-px bg-border" />
          <Dropdown.Item onSelect={() => { logout(); navigate("/login"); }}
            className="flex items-center gap-2 rounded-md px-2 h-8 text-sm outline-none data-[highlighted]:bg-surface-2 cursor-pointer text-danger">
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
  const item = "flex items-center gap-2 rounded-md px-2 h-9 text-sm cursor-pointer data-[selected=true]:bg-surface-2";
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
          {[...NAV, { to: "/connections/mcp", label: "MCP servers", icon: Plug }, { to: "/connections/agents", label: "Remote agents", icon: LayoutGrid },
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
          <Command.Item value="new run" onSelect={() => go("/")} className={item}><Sparkles className="size-4" aria-hidden /> New run</Command.Item>
          <Command.Item value="new council" onSelect={() => go("/councils/new")} className={item}><Users className="size-4" aria-hidden /> New council</Command.Item>
          {(["light", "dark", "system"] as Theme[]).map((t) => (
            <Command.Item key={t} value={`theme ${t}`} onSelect={() => { setTheme(t); setOpen(false); }} className={item}>
              {t === "dark" ? <Moon className="size-4" aria-hidden /> : t === "light" ? <Sun className="size-4" aria-hidden /> : <Monitor className="size-4" aria-hidden />}
              Theme: {t}
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

export function AppShell({ children }: { children?: ReactNode }) {
  const { navCollapsed, toggleNav } = useUi();
  const loc = useLocation();
  return (
    <div className="min-h-screen flex">
      <a href="#main" className="sr-only focus:not-sr-only focus:fixed focus:top-2 focus:left-2 focus:z-palette bg-surface px-3 py-2 rounded-md">
        Skip to content
      </a>
      <nav aria-label="Primary"
        className={cn("hidden md:flex flex-col border-r border-border bg-surface shrink-0 sticky top-0 h-screen z-rail transition-[width] duration-base",
          navCollapsed ? "w-16" : "w-60")}>
        <div className="flex items-center gap-2 h-12 px-3 border-b border-border">
          <img src="/favicon.svg" alt="" className="size-6" />
          {!navCollapsed && <span className="font-semibold">Argus Council</span>}
        </div>
        <ul className="flex flex-col gap-0.5 p-2">
          {NAV.map((n) => {
            const active = n.match ? loc.pathname.startsWith(n.match) : undefined;
            return (
              <li key={n.to}>
                <NavLink to={n.to} end={n.end} title={navCollapsed ? n.label : undefined}
                  className={({ isActive }) => cn("flex items-center gap-3 rounded-md h-9 px-3 text-sm",
                    (active ?? isActive) ? "bg-accent-subtle text-accent font-medium" : "text-fg-muted hover:bg-surface-2 hover:text-fg")}>
                  <n.icon className="size-4 shrink-0" aria-hidden />
                  {!navCollapsed && n.label}
                </NavLink>
              </li>
            );
          })}
        </ul>
        <ActiveRuns collapsed={navCollapsed} />
        <div className="mt-auto p-2">
          <IconButton label={navCollapsed ? "Expand navigation" : "Collapse navigation"} icon={navCollapsed ? PanelLeftOpen : PanelLeftClose} onClick={toggleNav} />
        </div>
      </nav>
      <div className="flex-1 min-w-0 flex flex-col">
        <header className="sticky top-0 z-header h-12 flex items-center gap-2 px-4 border-b border-border bg-surface/90 backdrop-blur">
          <span className="md:hidden font-semibold">Argus</span>
          <button onClick={() => window.dispatchEvent(new Event("argus:palette"))}
            className="ml-auto md:ml-0 flex items-center gap-2 h-8 px-3 rounded-md border border-border text-sm text-fg-subtle hover:bg-surface-2 w-auto md:w-72">
            <Search className="size-4" aria-hidden />
            <span className="hidden md:inline">Search or jump to…</span>
            <span className="hidden md:inline ml-auto"><Kbd>⌘K</Kbd></span>
          </button>
          <div className="md:ml-auto flex items-center gap-1">
            <ThemeMenu />
            <UserMenu />
          </div>
        </header>
        <main id="main" className="flex-1 min-w-0 pb-16 md:pb-0">{children ?? <Outlet />}</main>
      </div>
      <nav aria-label="Primary mobile" className="md:hidden fixed bottom-0 inset-x-0 z-header border-t border-border bg-surface grid grid-cols-4">
        {NAV.slice(0, 4).map((n) => (
          <NavLink key={n.to} to={n.to} end={n.end}
            className={({ isActive }) => cn("flex flex-col items-center justify-center h-14 text-2xs gap-0.5", isActive ? "text-accent" : "text-fg-muted")}>
            <n.icon className="size-5" aria-hidden /> {n.label}
          </NavLink>
        ))}
      </nav>
      <CommandPalette />
      <Toaster />
    </div>
  );
}
