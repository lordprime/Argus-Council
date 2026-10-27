// Role identity: color + glyph so role is never conveyed by color alone (ui-spec §7.3).

import {
  Blocks,
  Bot,
  Code2,
  FlaskConical,
  Gavel,
  Globe,
  MessageCircle,
  Network,
  Palette,
  PenLine,
  Radar,
  Scale,
  Search,
  Shield,
  User,
  type LucideIcon,
} from "lucide-react";

import { cn } from "@/lib/cn";

interface RoleStyle {
  icon: LucideIcon;
  fg: string;
  bg: string;
  bar: string;
  border: string;
}

// Literal class strings so Tailwind's JIT can see every colour (no dynamic class names).
export const ROLE_STYLES: Record<string, RoleStyle> = {
  chair: { icon: Gavel, fg: "text-amber-700 dark:text-amber-300", bg: "bg-amber-100 dark:bg-amber-900/60", bar: "bg-amber-700 dark:bg-amber-300", border: "border-l-amber-700 dark:border-l-amber-300" },
  researcher: { icon: Search, fg: "text-sky-700 dark:text-sky-300", bg: "bg-sky-100 dark:bg-sky-900/60", bar: "bg-sky-700 dark:bg-sky-300", border: "border-l-sky-700 dark:border-l-sky-300" },
  analyst: { icon: Network, fg: "text-violet-700 dark:text-violet-300", bg: "bg-violet-100 dark:bg-violet-900/60", bar: "bg-violet-700 dark:bg-violet-300", border: "border-l-violet-700 dark:border-l-violet-300" },
  writer: { icon: PenLine, fg: "text-teal-700 dark:text-teal-300", bg: "bg-teal-100 dark:bg-teal-900/60", bar: "bg-teal-700 dark:bg-teal-300", border: "border-l-teal-700 dark:border-l-teal-300" },
  architect: { icon: Blocks, fg: "text-blue-700 dark:text-blue-300", bg: "bg-blue-100 dark:bg-blue-900/60", bar: "bg-blue-700 dark:bg-blue-300", border: "border-l-blue-700 dark:border-l-blue-300" },
  coder: { icon: Code2, fg: "text-emerald-700 dark:text-emerald-300", bg: "bg-emerald-100 dark:bg-emerald-900/60", bar: "bg-emerald-700 dark:bg-emerald-300", border: "border-l-emerald-700 dark:border-l-emerald-300" },
  tester: { icon: FlaskConical, fg: "text-lime-700 dark:text-lime-300", bg: "bg-lime-100 dark:bg-lime-900/60", bar: "bg-lime-700 dark:bg-lime-300", border: "border-l-lime-700 dark:border-l-lime-300" },
  designer: { icon: Palette, fg: "text-fuchsia-700 dark:text-fuchsia-300", bg: "bg-fuchsia-100 dark:bg-fuchsia-900/60", bar: "bg-fuchsia-700 dark:bg-fuchsia-300", border: "border-l-fuchsia-700 dark:border-l-fuchsia-300" },
  assistant: { icon: MessageCircle, fg: "text-slate-600 dark:text-slate-300", bg: "bg-slate-100 dark:bg-slate-800/60", bar: "bg-slate-600 dark:bg-slate-300", border: "border-l-slate-600 dark:border-l-slate-300" },
  critic: { icon: Scale, fg: "text-orange-700 dark:text-orange-300", bg: "bg-orange-100 dark:bg-orange-900/60", bar: "bg-orange-700 dark:bg-orange-300", border: "border-l-orange-700 dark:border-l-orange-300" },
  monitor: { icon: Radar, fg: "text-cyan-700 dark:text-cyan-300", bg: "bg-cyan-100 dark:bg-cyan-900/60", bar: "bg-cyan-700 dark:bg-cyan-300", border: "border-l-cyan-700 dark:border-l-cyan-300" },
  security: { icon: Shield, fg: "text-rose-700 dark:text-rose-300", bg: "bg-rose-100 dark:bg-rose-900/60", bar: "bg-rose-700 dark:bg-rose-300", border: "border-l-rose-700 dark:border-l-rose-300" },
  remote: { icon: Globe, fg: "text-slate-600 dark:text-slate-300", bg: "bg-slate-100 dark:bg-slate-800/60", bar: "bg-slate-600 dark:bg-slate-300", border: "border-l-slate-600 dark:border-l-slate-300" },
  user: { icon: User, fg: "text-indigo-700 dark:text-indigo-300", bg: "bg-indigo-100 dark:bg-indigo-900/60", bar: "bg-indigo-700 dark:bg-indigo-300", border: "border-l-indigo-700 dark:border-l-indigo-300" },
};

const FALLBACK = Object.values(ROLE_STYLES).slice(0, 12);

export function roleStyle(role?: string): RoleStyle {
  if (!role) return { ...ROLE_STYLES.assistant!, icon: Bot };
  const known = ROLE_STYLES[role];
  if (known) return known;
  let h = 0;
  for (const c of role) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return { ...FALLBACK[h % FALLBACK.length]!, icon: Bot };
}

export function RoleAvatar({ role, overseer, size = 24, label }: { role?: string; overseer?: boolean; size?: number; label?: string }) {
  const s = roleStyle(role);
  return (
    <span
      role="img"
      aria-label={label ?? `${role ?? "agent"}${overseer ? " (overseer)" : ""}`}
      className={cn("inline-flex items-center justify-center rounded-full shrink-0", s.bg, s.fg,
        overseer && "outline outline-[1.5px] outline-dashed outline-offset-1 outline-current")}
      style={{ width: size, height: size }}
    >
      <s.icon style={{ width: size * 0.58, height: size * 0.58 }} aria-hidden />
    </span>
  );
}
