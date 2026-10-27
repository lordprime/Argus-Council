// Explicit seat editor used by the builder before the council exists.

import { ArrowDown, ArrowUp, Plus, Trash2, Users } from "lucide-react";
import { useRef } from "react";

import { Button, EmptyState, IconButton, SelectField, TextField } from "@/components/ui";
import { RoleAvatar } from "@/components/ui/role";
import { nextSeatName, roleTitle, validateName } from "@/features/councils/logic";
import type { RoleInfo } from "@/lib/types";

export interface SeatDraft {
  key: string;
  name: string;
  role: string;
}

let seq = 0;
export const draftKey = () => `seat-${++seq}`;

export function SeatDraftList({ seats, onChange, roles, max, onSeatDefaults, defaultsLabel, showErrors }: {
  seats: SeatDraft[];
  onChange: (s: SeatDraft[]) => void;
  roles: RoleInfo[];
  max: number;
  onSeatDefaults: () => void;
  defaultsLabel: string;
  showErrors?: boolean;
}) {
  const listRef = useRef<HTMLOListElement>(null);
  const overseers = new Set(roles.filter((r) => r.overseer).map((r) => r.key));

  const update = (key: string, patch: Partial<SeatDraft>) => onChange(seats.map((s) => (s.key === key ? { ...s, ...patch } : s)));
  const move = (i: number, d: -1 | 1, dir: "up" | "down") => {
    const next = [...seats];
    const [s] = next.splice(i, 1);
    next.splice(i + d, 0, s!);
    onChange(next);
    // Keep focus on the control the user pressed, or its sibling if it became disabled at the edge.
    requestAnimationFrame(() => {
      const target = i + d;
      const atEdge = (dir === "up" && target === 0) || (dir === "down" && target === next.length - 1);
      listRef.current?.querySelector<HTMLButtonElement>(`[data-move="${s!.key}-${atEdge ? (dir === "up" ? "down" : "up") : dir}"]`)?.focus();
    });
  };

  if (!seats.length) {
    return (
      <div className="rounded-lg border border-dashed border-border-strong">
        <EmptyState icon={Users} title="No seats yet"
          body="Start from the default roster for this mode, or add seats one at a time."
          action={
            <div className="flex flex-wrap justify-center gap-2">
              <Button type="button" variant="primary" onClick={onSeatDefaults}>{defaultsLabel}</Button>
              <Button type="button" icon={Plus} onClick={() => onChange([{ key: draftKey(), role: "assistant", name: nextSeatName([], "assistant", roles) }])}>
                Add seat
              </Button>
            </div>
          } />
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-3">
      <ol ref={listRef} className="flex flex-col gap-2" aria-label="Seats">
        {seats.map((s, i) => {
          const err = showErrors ? validateName(s.name) : undefined;
          return (
            <li key={s.key} className="flex flex-wrap sm:flex-nowrap items-start gap-3 rounded-lg border border-border bg-surface-2 p-3">
              <div className="flex items-center gap-2 pt-7">
                <span className="w-5 text-right text-xs text-fg-subtle tabular" aria-hidden>{i + 1}</span>
                <RoleAvatar role={s.role} overseer={overseers.has(s.role)} size={28} />
              </div>
              <TextField label={`Seat ${i + 1} name`} className="flex-1 min-w-40" value={s.name} maxLength={80} error={err}
                onChange={(e) => update(s.key, { name: e.target.value })} />
              <SelectField label={`Seat ${i + 1} role`} className="w-full sm:w-48" value={s.role}
                onChange={(e) => {
                  const role = e.target.value;
                  // Rename only if the user kept the auto-generated name.
                  const auto = s.name === roleTitle(roles, s.role) || s.name.startsWith(`${roleTitle(roles, s.role)} `);
                  const others = seats.filter((x) => x.key !== s.key);
                  update(s.key, { role, ...(auto ? { name: nextSeatName(others, role, roles) } : {}) });
                }}>
                {roles.map((r) => <option key={r.key} value={r.key}>{r.title}{r.overseer ? " (overseer)" : ""}</option>)}
              </SelectField>
              <div className="flex items-center gap-0.5 sm:pt-6 ml-auto">
                <IconButton label={`Move ${s.name || `seat ${i + 1}`} up`} icon={ArrowUp} disabled={i === 0} data-move={`${s.key}-up`}
                  onClick={() => move(i, -1, "up")} />
                <IconButton label={`Move ${s.name || `seat ${i + 1}`} down`} icon={ArrowDown} disabled={i === seats.length - 1}
                  data-move={`${s.key}-down`} onClick={() => move(i, 1, "down")} />
                <IconButton label={`Remove ${s.name || `seat ${i + 1}`}`} icon={Trash2} variant="danger-ghost"
                  onClick={() => onChange(seats.filter((x) => x.key !== s.key))} />
              </div>
            </li>
          );
        })}
      </ol>
      <div className="flex flex-wrap items-center gap-3">
        <Button type="button" size="sm" icon={Plus} disabled={seats.length >= max}
          onClick={() => onChange([...seats, { key: draftKey(), role: "assistant", name: nextSeatName(seats, "assistant", roles) }])}>
          Add seat
        </Button>
        {seats.length >= max && <span className="text-xs text-fg-subtle">At the maximum of {max} seats.</span>}
      </div>
    </div>
  );
}
