import { FileText, Layers } from "lucide-react";
import { Link } from "react-router";

import { Switch } from "@/components/connections/parts";
import { Card, CardTitle, TagList } from "@/components/ui";
import { useToggleSkill } from "@/features/skills/queries";
import { errorMessage } from "@/lib/api";
import type { SkillSummary } from "@/lib/types";
import { toast } from "@/stores/toast";

import { SkillBadges } from "./SkillBadges";

const MAX_TOOLS = 5;
const SOURCE_LABEL: Record<string, string> = { builtin: "Ships with Argus", upload: "Uploaded", marketplace: "From marketplace" };

export function SkillCard({ skill: s }: { skill: SkillSummary }) {
  const toggle = useToggleSkill();
  const id = `skill-${s.name}`;
  return (
    // A flex column whose footer sits on mt-auto, and a description clamped to exactly three lines with a
    // three-line minimum, so every card in a grid row lines its footer up (C-22).
    <Card as="article" aria-labelledby={id} className="p-4 flex flex-col gap-3 w-full">
      <div className="flex items-start gap-2">
        <div className="min-w-0 flex-1">
          <CardTitle as="h2" id={id}>
            <Link to={`/skills/${encodeURIComponent(s.name)}`} className="hover:underline underline-offset-2 break-all">{s.name}</Link>
          </CardTitle>
          <p className="text-xs text-fg-subtle font-mono">v{s.version}</p>
        </div>
        <SkillBadges skill={s} />
      </div>
      <p className="text-sm text-fg-muted line-clamp-3 min-h-[3lh]">{s.description}</p>
      {s.allowed_tools.length > 0 && <TagList items={s.allowed_tools} max={MAX_TOOLS} label={`Tools ${s.name} may use`} />}
      <div className="mt-auto flex items-center gap-3 pt-3 border-t border-border text-xs text-fg-subtle">
        <span className="inline-flex items-center gap-1"><FileText className="size-3.5" aria-hidden />{s.files.length} {s.files.length === 1 ? "file" : "files"}</span>
        <span className="inline-flex items-center gap-1"><Layers className="size-3.5" aria-hidden />{SOURCE_LABEL[s.source] ?? "Written here"}</span>
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
