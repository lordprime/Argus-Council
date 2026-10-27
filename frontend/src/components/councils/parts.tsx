// Small shared pieces of the Councils area.

import { Eye, Minus, Plus } from "lucide-react";
import { useState, type ReactNode } from "react";

import { ModeLabel } from "@/components/run/ModeLabel";
import { Button, Dialog, TextField } from "@/components/ui";
import { RoleAvatar } from "@/components/ui/role";
import { roleTitle } from "@/features/councils/logic";
import { cn } from "@/lib/cn";
import type { Member, RoleInfo } from "@/lib/types";

/** A council's mode: the shared ModeLabel badge, so councils and runs render mode identically. */
export function ModeBadge({ mode }: { mode: string }) {
  return <ModeLabel mode={mode} variant="badge" />;
}

export const isOverseer = (m: Pick<Member, "config" | "role">, overseers?: Set<string>) =>
  Boolean(m.config?.overseer ?? overseers?.has(m.role));

/** Overlapping avatars for a council's seats; overseers carry the dashed ring. */
export function AvatarStrip({ members, roles, max = 8 }: { members: Member[]; roles?: RoleInfo[]; max?: number }) {
  const shown = members.slice(0, max);
  const rest = members.length - shown.length;
  return (
    <ul className="flex items-center -space-x-1.5" aria-label="Seats">
      {shown.map((m) => (
        <li key={m.id} className={cn("rounded-full ring-2 ring-surface", !m.enabled && "opacity-50")}>
          <RoleAvatar role={m.role} overseer={isOverseer(m)} size={26}
            label={`${m.name}, ${roleTitle(roles, m.role)}${isOverseer(m) ? " (overseer)" : ""}${m.enabled ? "" : ", disabled"}`} />
        </li>
      ))}
      {rest > 0 && (
        <li className="inline-flex items-center justify-center rounded-full ring-2 ring-surface bg-surface-3 text-2xs text-fg-muted h-[26px] min-w-[26px] px-1 tabular">
          +{rest}
        </li>
      )}
    </ul>
  );
}

export function RoleChip({ role, roles, overseer, tone, children }: {
  role: string;
  roles?: RoleInfo[];
  overseer?: boolean;
  tone?: "add" | "remove";
  children?: ReactNode;
}) {
  return (
    <li className={cn("inline-flex items-center gap-1.5 rounded-full border pl-1 pr-2.5 h-7 text-sm",
      tone === "add" ? "border-success bg-success-subtle" : tone === "remove" ? "border-danger bg-danger-subtle line-through decoration-danger" : "border-border bg-surface-2")}>
      {tone === "add" && <Plus className="size-3.5 text-success" aria-label="Add" />}
      {tone === "remove" && <Minus className="size-3.5 text-danger" aria-label="Remove" />}
      <RoleAvatar role={role} overseer={overseer} size={20} />
      {children ?? roleTitle(roles, role)}
      {overseer && <Eye className="size-3.5 text-fg-subtle" aria-label="overseer" />}
    </li>
  );
}

/** Destructive confirm that names the object and requires typing its name (ui-spec §3.6, §8). */
export function ConfirmDeleteDialog({ open, onOpenChange, kind, name, body, onConfirm, pending, requireTyping = true, verb = "Delete" }: {
  open: boolean;
  onOpenChange: (o: boolean) => void;
  kind: string;
  name: string;
  body?: ReactNode;
  onConfirm: () => void;
  pending?: boolean;
  requireTyping?: boolean;
  verb?: string;
}) {
  const [typed, setTyped] = useState("");
  const ok = !requireTyping || typed.trim() === name;
  const change = (o: boolean) => {
    if (!o) setTyped("");
    onOpenChange(o);
  };
  return (
    <Dialog open={open} onOpenChange={change}
      title={`${verb} ${kind} “${name}”?`}
      description={requireTyping ? "This cannot be undone." : undefined}
      footer={
        <>
          <Button onClick={() => change(false)}>Cancel</Button>
          <Button variant="danger" loading={pending} disabled={!ok} onClick={onConfirm}>{verb} {kind}</Button>
        </>
      }>
      <form className="flex flex-col gap-3" onSubmit={(e) => { e.preventDefault(); if (ok) onConfirm(); }}>
        {body && <div className="text-sm text-fg-muted">{body}</div>}
        {requireTyping && (
          <TextField label={`Type “${name}” to confirm`} value={typed} onChange={(e) => setTyped(e.target.value)}
            autoComplete="off" spellCheck={false} autoFocus />
        )}
      </form>
    </Dialog>
  );
}
