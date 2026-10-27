import { CheckCircle2, Download, FileText, Search, Store } from "lucide-react";
import { useMemo, useState } from "react";
import { Link } from "react-router";

import { CardSkeletons } from "@/components/connections/parts";
import { Badge, Button, Card, EmptyState, ErrorState } from "@/components/ui";
import { useInstallSkill, useMarketplace, type MarketplaceSkill } from "@/features/skills/queries";
import { ApiError, errorMessage } from "@/lib/api";
import { toast } from "@/stores/toast";

function MarketplaceCard({ skill: s }: { skill: MarketplaceSkill }) {
  const install = useInstallSkill();
  const id = `market-${s.name}`;
  const onInstall = () =>
    install.mutate(s.name, {
      onSuccess: (r) => toast({ tone: "success", title: `Installed ${r.name}`, body: "It is now in your skills and available to councils." }),
      onError: (e) => toast({
        tone: "danger",
        title: `Could not install ${s.name}`,
        body: e instanceof ApiError && e.status === 409 ? `You already have a skill named ${s.name}.` : errorMessage(e),
      }),
    });
  return (
    <Card as="article" aria-labelledby={id} className="p-4 flex flex-col gap-3 w-full">
      <div className="flex items-start gap-2">
        <div className="min-w-0 flex-1">
          <h2 id={id} className="text-base font-semibold break-all">{s.name}</h2>
          <p className="text-xs text-fg-subtle font-mono">v{s.version}</p>
        </div>
        {s.installed && (
          <Badge tone="success"><CheckCircle2 className="size-3" aria-hidden />Installed</Badge>
        )}
      </div>
      <p className="text-sm text-fg-muted flex-1">{s.description}</p>
      {s.tags.length > 0 && (
        <ul aria-label={`Tags for ${s.name}`} className="flex flex-wrap gap-1">
          {s.tags.map((t) => <li key={t}><Badge>{t}</Badge></li>)}
        </ul>
      )}
      <div className="flex flex-wrap items-center gap-3 pt-3 border-t border-border text-xs text-fg-subtle">
        <span className="inline-flex items-center gap-1"><FileText className="size-3.5" aria-hidden />{s.files.length} {s.files.length === 1 ? "file" : "files"}</span>
        <span className="font-mono">{s.allowed_tools.length} tools</span>
        <div className="flex-1" />
        {s.installed ? (
          <Link to={`/skills/${encodeURIComponent(s.name)}`} className="text-sm text-accent underline underline-offset-2">
            Open<span className="sr-only"> {s.name}</span>
          </Link>
        ) : (
          <Button size="sm" variant="primary" icon={Download} loading={install.isPending} onClick={onInstall}>
            Install<span className="sr-only"> {s.name}</span>
          </Button>
        )}
      </div>
    </Card>
  );
}

/** Curated, in-repo skill catalogue. Installing copies the package into the user's own skills. */
export function Marketplace() {
  const q = useMarketplace();
  const [query, setQuery] = useState("");
  const visible = useMemo(() => {
    const t = query.trim().toLowerCase();
    return (q.data ?? []).filter((s) => !t || s.name.includes(t) || s.description.toLowerCase().includes(t) || s.tags.some((g) => g.includes(t)));
  }, [q.data, query]);

  return (
    <div className="flex flex-col gap-4 pt-4">
      <p className="text-sm text-fg-muted">
        Curated skills reviewed with Argus. Installing copies one into your skills, where you can edit it; updates here never change your copy.
      </p>
      <div className="flex flex-col gap-1 max-w-md">
        <label htmlFor="market-search" className="text-sm font-medium">Search marketplace</label>
        <div className="relative">
          <Search className="size-4 text-fg-subtle absolute left-3 top-1/2 -translate-y-1/2" aria-hidden />
          <input id="market-search" type="search" value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Name, description or tag"
            className="w-full h-9 rounded-md border border-border-strong bg-surface pl-9 pr-3 text-base placeholder:text-fg-subtle" />
        </div>
      </div>
      <p className="sr-only" role="status" aria-live="polite">{q.data ? `${visible.length} of ${q.data.length} marketplace skills shown` : ""}</p>
      {q.isLoading ? (
        <CardSkeletons count={6} className="lg:grid-cols-3" />
      ) : q.error ? (
        <Card><ErrorState error={q.error} onRetry={() => void q.refetch()} /></Card>
      ) : !visible.length ? (
        <Card as="div">
          <EmptyState icon={Store} title={q.data?.length ? "No marketplace skills match" : "The marketplace is empty"}
            body={query ? <>Nothing matches “{query}”.</> : undefined}
            action={query ? <Button size="sm" onClick={() => setQuery("")}>Clear search</Button> : undefined} />
        </Card>
      ) : (
        <ul className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
          {visible.map((s) => <li key={s.name} className="flex"><MarketplaceCard skill={s} /></li>)}
        </ul>
      )}
    </div>
  );
}
