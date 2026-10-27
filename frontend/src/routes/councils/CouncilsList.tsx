import { Plus, Trash2, Users } from "lucide-react";
import { useMemo, useState } from "react";
import { Link } from "react-router";

import { AvatarStrip, ConfirmDeleteDialog, isOverseer, ModeBadge } from "@/components/councils/parts";
import { ButtonLink, Card, CardTitle, EmptyState, ErrorState, IconButton, Page, PageHeader, Skeleton } from "@/components/ui";
import { useDeleteCouncil } from "@/features/councils/queries";
import { errorMessage } from "@/lib/api";
import { relTime } from "@/lib/format";
import { useCouncils, useRoles, useRunsList } from "@/lib/queries";
import type { Council } from "@/lib/types";
import { toast } from "@/stores/toast";

function NewCouncilLink({ label = "New council", variant = "primary" }: { label?: string; variant?: "primary" | "secondary" }) {
  return <ButtonLink to="/councils/new" variant={variant} icon={Plus}>{label}</ButtonLink>;
}

function CouncilCard({ council, lastRun, onDelete }: { council: Council; lastRun?: string; onDelete: () => void }) {
  const roles = useRoles();
  const overseers = council.members.filter((m) => isOverseer(m)).length;
  const seats = council.members.length;
  return (
    <Card as="article" className="relative flex flex-col gap-3 p-4 hover:border-border-strong transition-colors duration-fast focus-within:border-border-strong"
      aria-labelledby={`council-${council.id}`}>
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <CardTitle as="h2" id={`council-${council.id}`} className="truncate">
            {/* Stretched link: the whole card is the click target, actions sit above it. */}
            <Link to={`/councils/${council.id}`} className="after:absolute after:inset-0 after:rounded-lg focus-visible:outline-none focus-visible:after:outline focus-visible:after:outline-2 focus-visible:after:outline-offset-2 focus-visible:after:outline-[rgb(var(--focus-ring))]">
              {council.name}
            </Link>
          </CardTitle>
          <div className="mt-1"><ModeBadge mode={council.mode} /></div>
        </div>
        <IconButton label={`Delete ${council.name}`} icon={Trash2} variant="danger-ghost" className="relative z-[1] -mr-1 -mt-1"
          onClick={onDelete} />
      </div>
      <p className="text-sm text-fg-muted line-clamp-2 min-h-10">
        {council.description || <span className="text-fg-subtle">No description</span>}
      </p>
      <AvatarStrip members={council.members} roles={roles.data?.roles} />
      <dl className="mt-auto grid grid-cols-3 gap-2 border-t border-border pt-3 text-xs">
        <div>
          <dt className="text-fg-subtle">Seats</dt>
          <dd className="tabular text-fg">{seats} <span className="text-fg-subtle">({council.min_size}–{council.max_size})</span></dd>
        </div>
        <div>
          <dt className="text-fg-subtle">Overseers</dt>
          <dd className="tabular text-fg">{overseers}</dd>
        </div>
        <div>
          <dt className="text-fg-subtle">{lastRun ? "Last run" : "Created"}</dt>
          <dd className="text-fg">
            <time dateTime={lastRun ?? council.created_at} title={new Date(lastRun ?? council.created_at).toLocaleString()}>
              {relTime(lastRun ?? council.created_at)}
            </time>
          </dd>
        </div>
      </dl>
    </Card>
  );
}

export default function CouncilsList() {
  const councils = useCouncils();
  const runs = useRunsList({ limit: 100 });
  const del = useDeleteCouncil();
  const [target, setTarget] = useState<Council | null>(null);

  const lastRun = useMemo(() => {
    const out = new Map<string, string>();
    for (const r of runs.data ?? []) {
      if (!r.council_id) continue;
      const prev = out.get(r.council_id);
      if (!prev || prev < r.created_at) out.set(r.council_id, r.created_at);
    }
    return out;
  }, [runs.data]);

  const confirmDelete = async () => {
    if (!target) return;
    const name = target.name;
    try {
      await del.mutateAsync(target.id);
      setTarget(null);
      toast({ tone: "success", title: `Deleted “${name}”` });
    } catch (e) {
      toast({ tone: "danger", title: `Could not delete “${name}”`, body: errorMessage(e), action: { label: "Retry", run: () => void confirmDelete() } });
    }
  };

  const list = councils.data ?? [];
  return (
    <Page>
      {/* The header action always stays (C-23); the empty state offers it again only as a secondary CTA. */}
      <PageHeader title="Councils"
        description="Reusable teams of agents: seats, roles, models and the guardrails they work inside."
        actions={<NewCouncilLink />} />

      {councils.isLoading ? (
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3" aria-busy="true" aria-label="Loading councils">
          {[0, 1, 2].map((i) => (
            <div key={i} className="rounded-lg border border-border bg-surface p-4 flex flex-col gap-3">
              <Skeleton className="h-5 w-40" />
              <Skeleton className="h-4 w-20" />
              <Skeleton className="h-10" />
              <Skeleton className="h-7 w-48" />
              <Skeleton className="h-9" />
            </div>
          ))}
        </div>
      ) : councils.error ? (
        <Card as="div"><ErrorState error={councils.error} onRetry={() => void councils.refetch()} /></Card>
      ) : list.length === 0 ? (
        <Card as="div">
          <EmptyState icon={Users} title="No councils yet"
            body="A council is a saved team: who sits on it, which models they use and what they may do. Runs can also build one on the fly from a mode."
            action={<NewCouncilLink label="Create your first council" variant="secondary" />} />
        </Card>
      ) : (
        <ul className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3" aria-label="Councils">
          {list.map((c) => (
            <li key={c.id} className="flex">
              <div className="flex-1 flex flex-col [&>article]:flex-1">
                <CouncilCard council={c} lastRun={lastRun.get(c.id)} onDelete={() => setTarget(c)} />
              </div>
            </li>
          ))}
        </ul>
      )}

      <ConfirmDeleteDialog open={target != null} onOpenChange={(o) => !o && setTarget(null)}
        kind="council" name={target?.name ?? ""} pending={del.isPending} onConfirm={() => void confirmDelete()}
        body={target && <>Its {target.members.length} seats and guardrails are removed. Past runs keep their own snapshot.</>} />
    </Page>
  );
}

