import { History, KeyRound, Plus, Send, Trash2, Webhook as WebhookIcon } from "lucide-react";
import { useState } from "react";

import { Chips, Switch } from "@/components/connections/parts";
import { SettingsLayout } from "@/components/settings/SettingsNav";
import { Badge, Button, Callout, Card, Dialog, EmptyState, ErrorState, IconButton, Skeleton } from "@/components/ui";
import {
  CreateWebhookDialog,
  DeliveriesDrawer,
  DeliveryOutcome,
  fullDate,
  SecretDialog,
  TypedConfirmDelete,
} from "@/features/webhooks/components";
import {
  useDeleteWebhook,
  useRotateWebhookSecret,
  useTestWebhook,
  useUpdateWebhook,
  useWebhooks,
  webhookHost,
  type Webhook,
  type WebhookWithSecret,
} from "@/features/webhooks/queries";
import { errorMessage } from "@/lib/api";
import { relTime } from "@/lib/format";
import { toast } from "@/stores/toast";

function LastStatus({ h }: { h: Webhook }) {
  if (h.last_status == null) return <span className="text-fg-muted">Never delivered</span>;
  const code = Number(h.last_status);
  const ok = Number.isFinite(code) && code >= 200 && code < 300;
  return <DeliveryOutcome ok={ok} label={Number.isFinite(code) ? `HTTP ${code}` : String(h.last_status)} />;
}

function WebhookRow({ h, onDeliveries, onRotate, onDelete }: {
  h: Webhook;
  onDeliveries: () => void;
  onRotate: () => void;
  onDelete: () => void;
}) {
  const update = useUpdateWebhook();
  const test = useTestWebhook();
  const host = webhookHost(h.url);
  const headingId = `hook-${h.id}`;

  const toggle = (enabled: boolean) =>
    update.mutate({ id: h.id, patch: { enabled } }, {
      onError: (e) => toast({ tone: "danger", title: `Could not update ${host}`, body: errorMessage(e) }),
    });

  return (
    <li className="flex flex-col gap-3 px-4 py-4">
      <div className="flex items-start gap-3">
        <WebhookIcon className="size-4 mt-1 text-fg-subtle shrink-0" aria-hidden />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <h3 id={headingId} className="font-medium truncate">{host}</h3>
            {!h.enabled && <Badge tone="neutral">Disabled</Badge>}
            {h.failure_count > 0 && <Badge tone="warning">{h.failure_count} consecutive failures</Badge>}
          </div>
          <p className="font-mono text-xs text-fg-muted break-all">{h.url}</p>
          {h.description && <p className="text-sm text-fg-muted mt-0.5">{h.description}</p>}
        </div>
      </div>
      <dl className="grid gap-3 sm:grid-cols-3 text-sm">
        <div className="min-w-0 sm:col-span-3">
          <dt className="text-xs text-fg-subtle mb-1">Events</dt>
          <dd><Chips items={h.events} label={`Events for ${host}`} /></dd>
        </div>
        <div>
          <dt className="text-xs text-fg-subtle">Last status</dt>
          <dd><LastStatus h={h} /></dd>
        </div>
        <div>
          <dt className="text-xs text-fg-subtle">Failures</dt>
          <dd className="tabular">{h.failure_count}</dd>
        </div>
        <div>
          <dt className="text-xs text-fg-subtle">Last delivery</dt>
          <dd className="tabular">
            {h.last_delivery_at
              ? <time dateTime={h.last_delivery_at} title={fullDate(h.last_delivery_at)}>{relTime(h.last_delivery_at)}</time>
              : <span className="text-fg-muted">Never</span>}
          </dd>
        </div>
      </dl>
      {test.data && (
        <Callout tone={test.data.ok ? "success" : "danger"}
          title={test.data.ok ? "Test delivered" : "Test delivery failed"}>
          {test.data.ok
            ? `The receiver accepted the ping${test.data.last_status != null ? ` (HTTP ${test.data.last_status})` : ""}.`
            : `Failure count is now ${test.data.failure_count}. Open recent deliveries for the error.`}
        </Callout>
      )}
      {test.error != null && <Callout tone="danger" title="Could not send the test">{errorMessage(test.error)}</Callout>}
      <div className="flex flex-wrap items-center gap-2 pt-3 border-t border-border">
        <Switch object={host} checked={h.enabled} onChange={toggle} disabled={update.isPending} />
        <div className="flex-1" />
        <Button size="sm" icon={Send} loading={test.isPending} aria-label={`Send test to ${host}`} onClick={() => test.mutate(h.id)}>
          Send test
        </Button>
        <Button size="sm" icon={History} aria-label={`Recent deliveries for ${host}`} onClick={onDeliveries}>Deliveries</Button>
        <Button size="sm" variant="ghost" icon={KeyRound} aria-label={`Rotate secret for ${host}`} onClick={onRotate}>Rotate secret</Button>
        <IconButton label={`Delete ${host}`} icon={Trash2} variant="danger-ghost" onClick={onDelete} />
      </div>
    </li>
  );
}

function RotateDialog({ target, onClose, onRotated }: { target: Webhook | null; onClose: () => void; onRotated: (h: WebhookWithSecret) => void }) {
  const rotate = useRotateWebhookSecret();
  const host = target ? webhookHost(target.url) : "";
  return (
    <Dialog open={Boolean(target)} onOpenChange={(o) => { if (!o) { rotate.reset(); onClose(); } }}
      title={`Rotate the secret for ${host}?`}
      description="The current secret stops working immediately. Update your receiver with the new one."
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          <Button variant="primary" icon={KeyRound} loading={rotate.isPending}
            onClick={() => target && rotate.mutate(target.id, { onSuccess: (h) => { onClose(); onRotated(h); } })}>
            Rotate secret
          </Button>
        </>
      }>
      {rotate.error != null && <Callout tone="danger" title="Could not rotate the secret">{errorMessage(rotate.error)}</Callout>}
    </Dialog>
  );
}

export default function Webhooks() {
  const q = useWebhooks();
  const del = useDeleteWebhook();
  const [creating, setCreating] = useState(false);
  const [drawer, setDrawer] = useState<Webhook | null>(null);
  const [rotating, setRotating] = useState<Webhook | null>(null);
  const [revealed, setRevealed] = useState<WebhookWithSecret | null>(null);
  const [deleting, setDeleting] = useState<Webhook | null>(null);
  const hooks = q.data ?? [];
  const add = <Button variant="primary" icon={Plus} onClick={() => setCreating(true)}>Add webhook</Button>;

  return (
    <SettingsLayout title="Webhooks"
      description="Get signed HTTP callbacks when runs start, finish, fail or need approval."
      actions={add}>
      {q.isLoading ? (
        <div role="status" aria-label="Loading webhooks"><Card as="div" className="p-4 space-y-3"><Skeleton className="h-5 w-48" /><Skeleton className="h-16" /></Card></div>
      ) : q.error ? (
        <Card as="div"><ErrorState error={q.error} onRetry={() => void q.refetch()} /></Card>
      ) : hooks.length === 0 ? (
        <Card as="div">
          <EmptyState icon={WebhookIcon} title="No webhooks yet"
            body="Add an endpoint to notify Slack, CI or your own service when councils finish." />
        </Card>
      ) : (
        <section aria-labelledby="hooks-h">
          <h2 id="hooks-h" className="text-lg mb-2">Endpoints <span className="text-fg-subtle font-normal tabular">({hooks.length})</span></h2>
          <Card as="div">
            <ul className="divide-y divide-border">
              {hooks.map((h) => (
                <WebhookRow key={h.id} h={h} onDeliveries={() => setDrawer(h)} onRotate={() => setRotating(h)}
                  onDelete={() => { del.reset(); setDeleting(h); }} />
              ))}
            </ul>
          </Card>
        </section>
      )}

      {creating && <CreateWebhookDialog open onOpenChange={setCreating} />}
      <RotateDialog target={rotating} onClose={() => setRotating(null)} onRotated={setRevealed} />
      <SecretDialog hook={revealed} title="New signing secret" onClose={() => setRevealed(null)} />
      <DeliveriesDrawer hook={drawer} onClose={() => setDrawer(null)} />
      {deleting && (
        <TypedConfirmDelete open onOpenChange={(o) => !o && setDeleting(null)} kind="webhook" name={webhookHost(deleting.url)}
          consequence={<>Argus stops sending events to <span className="font-mono break-all">{deleting.url}</span> and discards its delivery history.</>}
          pending={del.isPending} error={del.error}
          onConfirm={() => del.mutate(deleting.id, {
            onSuccess: () => { toast({ tone: "success", title: `Deleted ${webhookHost(deleting.url)}` }); setDeleting(null); },
          })} />
      )}
    </SettingsLayout>
  );
}
