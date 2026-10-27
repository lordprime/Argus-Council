import { Code2, FileText, MessageCircle, Wand2 } from "lucide-react";
import { Link } from "react-router";

import { StatusPill } from "@/components/ui";
import { duration, relTime, usd } from "@/lib/format";
import type { Mode, Run } from "@/lib/types";

const MODE_ICON: Record<Mode, typeof FileText> = { research: FileText, coding: Code2, chat: MessageCircle, general: Wand2 };

export function RunRow({ run }: { run: Run }) {
  const Icon = MODE_ICON[run.mode] ?? Wand2;
  const elapsed = run.usage?.runtime_s ?? (run.started_at ? (Date.now() - new Date(run.started_at).getTime()) / 1000 : null);
  return (
    <li>
      <Link to={`/runs/${run.id}`} className="flex items-center gap-3 px-4 py-3 hover:bg-surface-2 focus-visible:bg-surface-2">
        <Icon className="size-4 text-fg-subtle shrink-0" aria-label={run.mode} />
        <div className="min-w-0 flex-1">
          <p className="truncate font-medium">{run.topic}</p>
          <p className="text-xs text-fg-subtle tabular">
            {run.mode} · {relTime(run.created_at)} · {duration(elapsed)} · {usd(run.usage?.usd)}
          </p>
        </div>
        <StatusPill status={run.status} />
      </Link>
    </li>
  );
}
