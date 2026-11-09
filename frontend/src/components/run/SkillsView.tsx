// The Skills view of a run: every skill the council loaded, grouped by name, with who loaded it and
// when, the skill files they read, and how many tool calls ran with it in effect.

import { Sparkles } from "lucide-react";
import { useMemo, useState } from "react";

import { EmptyState, FilterBar, SelectField } from "@/components/ui";
import { Tag } from "@/components/ui/patterns";
import { clock } from "@/lib/format";
import type { RunState, SkillUse } from "@/stores/run";

type Group = { name: string; versions: string[]; loads: SkillUse[]; reads: SkillUse[]; calls: number };

export function SkillsView({ run }: { run: RunState }) {
  const [member, setMember] = useState("");
  const skills = useMemo(() => run.skills ?? [], [run.skills]);
  const nameOf = (id: string) => run.members.find((m) => m.id === id)?.name ?? id;

  const groups = useMemo(() => {
    const byName = new Map<string, Group>();
    const get = (name: string) => {
      let g = byName.get(name);
      if (!g) byName.set(name, (g = { name, versions: [], loads: [], reads: [], calls: 0 }));
      return g;
    };
    for (const u of skills) {
      if (member && u.memberId !== member) continue;
      const g = get(u.name);
      if (u.version && !g.versions.includes(u.version)) g.versions.push(u.version);
      (u.kind === "loaded" ? g.loads : g.reads).push(u);
    }
    for (const it of Object.values(run.items)) {
      if (it.kind !== "tool" || !it.activeSkills?.length) continue;
      if (member && it.actor?.member_id !== member) continue;
      for (const n of it.activeSkills) if (byName.has(n)) byName.get(n)!.calls += 1;
    }
    return [...byName.values()];
  }, [skills, run.items, member]);

  const memberIds = [...new Set(skills.map((u) => u.memberId))];

  if (skills.length === 0) {
    return <EmptyState icon={Sparkles} title="No skills used yet" body="Skills the council loads, and the files they read, show up here." />;
  }
  return (
    <div className="flex flex-col gap-4 p-4">
      <FilterBar label="Filter skills" canClear={member !== ""} onClear={() => setMember("")}>
        <SelectField label="Member" value={member} onChange={(e) => setMember(e.target.value)} className="w-full sm:w-48">
          <option value="">All members</option>
          {memberIds.map((id) => <option key={id} value={id}>{nameOf(id)}</option>)}
        </SelectField>
      </FilterBar>
      {groups.map((g) => {
        const files = [...new Set(g.reads.map((r) => r.path ?? ""))].filter(Boolean);
        return (
          <section key={g.name} aria-label={g.name} className="rounded-lg border border-border bg-surface p-3 flex flex-col gap-2">
            <h3 className="text-sm font-semibold flex items-center gap-2">
              <Sparkles className="size-4 text-fg-subtle" aria-hidden />
              <span className="font-mono">{g.name}</span>
              {g.versions.length > 0 && <span className="text-xs font-normal text-fg-subtle">v{g.versions.join(", v")}</span>}
            </h3>
            <ul className="flex flex-col gap-1 text-sm">
              {g.loads.map((l) => (
                <li key={l.seq} className="flex gap-2">
                  <span>{nameOf(l.memberId)}</span>
                  <span className="text-fg-subtle tabular">loaded at {clock(l.ts)}</span>
                </li>
              ))}
            </ul>
            {files.length > 0 && (
              <div className="flex flex-wrap items-center gap-1.5 text-sm">
                <span className="text-fg-muted">Files read</span>
                {files.map((f) => <Tag key={f}>{f}</Tag>)}
              </div>
            )}
            <p className="text-xs text-fg-subtle">{g.calls} tool call{g.calls === 1 ? "" : "s"} ran with this skill in effect</p>
          </section>
        );
      })}
    </div>
  );
}
