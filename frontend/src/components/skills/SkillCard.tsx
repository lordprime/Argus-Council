import { FileText, Layers } from "lucide-react";
import { Link } from "react-router";

import { Switch } from "@/components/connections/parts";
import { Card } from "@/components/ui";
import { useToggleSkill } from "@/features/skills/queries";
import { errorMessage } from "@/lib/api";
import type { SkillSummary } from "@/lib/types";
import { toast } from "@/stores/toast";

import { SkillBadges } from "./SkillBadges";

const MAX_TOOLS = 5;

export function SkillCard({ skill: s }: { skill: SkillSummary }) {
  const toggle = useToggleSkill();
  const id = `skill-${s.name}`;
  const extra = s.allowed_tools.length - MAX_TOOLS;
  return (
    <Card as="article" aria-labelledby={id} className="p-4 flex flex-col gap-3 w-full">
      <div className="flex items-start gap-2">
        <div className="min-w-0 flex-1">
          <h2 id={id} className="text-base font-semibold">
            <Link to={`/skills/${encodeURIComponent(s.name)}`} className="hover:underline underline-offset-2 break-all">{s.name}</Link>
          </h2>
          <p className="text-xs text-fg-subtle font-mono">v{s.version}</p>
        </div>
        <SkillBadges skill={s} />
      </div>
      <p className="text-sm text-fg-muted line-clamp-3 flex-1">{s.description}</p>
      {s.allowed_tools.length > 0 && (
        <ul aria-label={`Tools ${s.name} may use`} className="flex flex-wrap gap-1">
          {s.allowed_tools.slice(0, MAX_TOOLS).map((t) => (
            <li key={t} className="inline-flex items-center h-6 rounded-sm border border-border bg-surface-2 px-1.5 font-mono text-xs text-fg-muted">{t}</li>
          ))}
          {extra > 0 && <li className="inline-flex items-center h-6 px-1 text-xs text-fg-subtle">+{extra} more</li>}
        </ul>
      )}
      <div className="flex items-center gap-3 pt-3 border-t border-border text-xs text-fg-subtle">
        <span className="inline-flex items-center gap-1"><FileText className="size-3.5" aria-hidden />{s.files.length} {s.files.length === 1 ? "file" : "files"}</span>
        <span className="inline-flex items-center gap-1"><Layers className="size-3.5" aria-hidden />{s.source === "builtin" ? "Ships with Argus" : s.source === "upload" ? "Uploaded" : "Written here"}</span>
        <div className="flex-1" />
        {s.builtin ? (
          <span className="text-fg-muted">Always available</span>
        ) : (
          <Switch object={s.name} checked={s.enabled} disabled={toggle.isPending}
            onChange={(enabled) => toggle.mutate({ name: s.name, enabled }, {
              onError: (e) => toast({ tone: "danger", title: `Could not update ${s.name}`, body: errorMessage(e) }),
            })} />
        )}
      </div>
    </Card>
  );
}
