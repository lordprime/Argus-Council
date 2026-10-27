import { KeyRound, Plus, Trash2 } from "lucide-react";
import { useState } from "react";

import { CreateApiKeyDialog } from "@/components/settings/CreateApiKeyDialog";
import { SettingsLayout } from "@/components/settings/SettingsNav";
import { Badge, Button, Callout, Card, Dialog, EmptyState, ErrorState, Skeleton } from "@/components/ui";
import { errorMessage } from "@/lib/api";
import { relTime } from "@/lib/format";
import { useApiKeys } from "@/lib/queries";
import type { ApiKeyInfo } from "@/lib/types";
import { useRevokeApiKey } from "@/features/settings/queries";
import { describeScopes } from "@/features/settings/scopes";
import { toast } from "@/stores/toast";

const isExpired = (k: ApiKeyInfo) => Boolean(k.expires_at && new Date(k.expires_at).getTime() < Date.now());
const fullDate = (iso: string) => new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });

function KeyStatus({ k }: { k: ApiKeyInfo }) {
  if (k.revoked) return <Badge tone="neutral">Revoked</Badge>;
  if (isExpired(k)) return <Badge tone="warning">Expired</Badge>;
  return <Badge tone="success">Active</Badge>;
}

function Meta({ label, iso, empty }: { label: string; iso: string | null; empty: string }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs text-fg-subtle">{label}</dt>
      <dd className="text-sm tabular">
        {iso ? <time dateTime={iso} title={fullDate(iso)}>{relTime(iso)}</time> : <span className="text-fg-muted">{empty}</span>}
      </dd>
    </div>
  );
}

function KeyRow({ k, onRevoke }: { k: ApiKeyInfo; onRevoke?: (k: ApiKeyInfo) => void }) {
  const dead = k.revoked || isExpired(k);
  return (
    <li className="flex flex-col gap-3 px-4 py-3.5 sm:flex-row sm:items-center sm:gap-6">
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <KeyRound className="size-4 text-fg-subtle shrink-0" aria-hidden />
          <p className={dead ? "truncate font-medium text-fg-muted" : "truncate font-medium"}>{k.name}</p>
          <KeyStatus k={k} />
        </div>
        <p className="mt-1 flex flex-wrap items-center gap-x-1.5 gap-y-1 text-xs text-fg-muted">
          <code className="font-mono rounded-sm bg-surface-2 px-1" title="Key prefix">{k.prefix}…</code>
          <span className="whitespace-nowrap"><span aria-hidden>· </span>{describeScopes(k.scopes)}</span>
          <span className="whitespace-nowrap">
            <span aria-hidden>· </span>Created <time dateTime={k.created_at} title={fullDate(k.created_at)}>{relTime(k.created_at)}</time>
          </span>
        </p>
      </div>
      <div className="flex items-center gap-4 sm:gap-6">
        <dl className="grid grid-cols-2 gap-4 sm:w-48">
          <Meta label="Last used" iso={k.last_used_at} empty="Never" />
          <Meta label={isExpired(k) ? "Expired" : "Expires"} iso={k.expires_at} empty="Never" />
        </dl>
        {onRevoke && (
          <Button variant="danger-ghost" size="sm" icon={Trash2} aria-label={`Revoke ${k.name}`} onClick={() => onRevoke(k)}
            className="ml-auto sm:ml-0">
            Revoke
          </Button>
        )}
      </div>
    </li>
  );
}

function RevokeDialog({ target, onClose }: { target: ApiKeyInfo | null; onClose: () => void }) {
  const revoke = useRevokeApiKey();
  const confirm = async () => {
    if (!target) return;
    try {
      await revoke.mutateAsync(target.id);
      toast({ tone: "success", title: `Revoked “${target.name}”` });
      onClose();
    } catch {
      /* shown inline */
    }
  };
  return (
    <Dialog open={Boolean(target)} onOpenChange={(o) => { if (!o) { revoke.reset(); onClose(); } }}
      title={target ? `Revoke “${target.name}”?` : "Revoke key?"}
      description={target ? `Anything using the key ${target.prefix}… stops working immediately. This can't be undone.` : undefined}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          <Button variant="danger" icon={Trash2} loading={revoke.isPending} onClick={confirm}>Revoke key</Button>
        </>
      }>
      {revoke.error ? <Callout tone="danger" title="Could not revoke the key">{errorMessage(revoke.error)}</Callout> : null}
    </Dialog>
  );
}

function ListSkeleton() {
  return (
    <Card as="div" className="divide-y divide-border" aria-hidden>
      {[0, 1, 2].map((i) => (
        <div key={i} className="flex flex-col sm:flex-row gap-3 sm:gap-6 px-4 py-4">
          <div className="flex-1 space-y-2"><Skeleton className="h-4 w-40" /><Skeleton className="h-3 w-56" /></div>
          <div className="sm:w-48 grid grid-cols-2 gap-4"><Skeleton className="h-8" /><Skeleton className="h-8" /></div>
        </div>
      ))}
    </Card>
  );
}

export default function ApiKeys() {
  const q = useApiKeys();
  const [creating, setCreating] = useState(false);
  const [revoking, setRevoking] = useState<ApiKeyInfo | null>(null);

  const keys = q.data ?? [];
  const live = keys.filter((k) => !k.revoked && !isExpired(k));
  const dead = keys.filter((k) => k.revoked || isExpired(k));

  return (
    <SettingsLayout title="API keys"
      description="Let scripts, CI and Claude Code use Argus as you. Keys are shown once, at creation."
      actions={<Button variant="primary" icon={Plus} onClick={() => setCreating(true)}>Create key</Button>}>
      {q.isLoading ? (
        <div role="status" aria-label="Loading API keys"><ListSkeleton /></div>
      ) : q.error ? (
        <Card as="div"><ErrorState error={q.error} onRetry={() => void q.refetch()} /></Card>
      ) : live.length === 0 && dead.length === 0 ? (
        <Card as="div">
          <EmptyState icon={KeyRound} title="No API keys yet"
            body="Create a key to connect Claude Code over MCP or to call the REST API from your own tools."
            action={<Button variant="primary" icon={Plus} onClick={() => setCreating(true)}>Create your first key</Button>} />
        </Card>
      ) : (
        <>
          <section aria-labelledby="active-keys-h">
            <h2 id="active-keys-h" className="text-lg mb-2">Active keys <span className="text-fg-subtle font-normal tabular">({live.length})</span></h2>
            {live.length ? (
              <Card as="div"><ul className="divide-y divide-border">{live.map((k) => <KeyRow key={k.id} k={k} onRevoke={setRevoking} />)}</ul></Card>
            ) : (
              <Card as="div"><EmptyState icon={KeyRound} title="No active keys" body="Every key you created has been revoked or has expired." /></Card>
            )}
          </section>
          {dead.length > 0 && (
            <details className="group">
              <summary className="cursor-pointer text-sm text-fg-muted hover:text-fg w-fit rounded-sm">
                Revoked and expired keys ({dead.length})
              </summary>
              <Card as="div" className="mt-2">
                <ul className="divide-y divide-border">{dead.map((k) => <KeyRow key={k.id} k={k} />)}</ul>
              </Card>
            </details>
          )}
        </>
      )}

      <CreateApiKeyDialog open={creating} onOpenChange={setCreating} />
      <RevokeDialog target={revoking} onClose={() => setRevoking(null)} />
    </SettingsLayout>
  );
}
