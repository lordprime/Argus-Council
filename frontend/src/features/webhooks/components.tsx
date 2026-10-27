import * as RDialog from "@radix-ui/react-dialog";
import { Check, CheckCircle2, Copy, History, RefreshCw, Trash2, TriangleAlert, Webhook as WebhookIcon, X, XCircle } from "lucide-react";
import { useId, useState, type FormEvent, type ReactNode } from "react";

import { Badge, Button, Callout, Dialog, EmptyState, ErrorState, IconButton, Skeleton, TextField } from "@/components/ui";
import { errorMessage } from "@/lib/api";
import { cn } from "@/lib/cn";
import { relTime } from "@/lib/format";

import {
  useCreateWebhook,
  useWebhookDeliveries,
  useWebhookEvents,
  webhookHost,
  type Webhook,
  type WebhookWithSecret,
} from "./queries";

export const fullDate = (iso: string) => new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });

function CopyButton({ text, label }: { text: string; label: string }) {
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");
  return (
    <Button type="button" size="sm" icon={state === "copied" ? Check : Copy} aria-label={label} className="shrink-0"
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(text);
          setState("copied");
        } catch {
          setState("failed");
        }
        window.setTimeout(() => setState("idle"), 2000);
      }}>
      <span aria-live="polite">{state === "copied" ? "Copied" : state === "failed" ? "Select and copy" : "Copy"}</span>
    </Button>
  );
}

/** The signing secret, shown exactly once. Done stays disabled until the user confirms they stored it. */
function SecretReveal({ hook, onDone }: { hook: WebhookWithSecret; onDone: () => void }) {
  const ackId = useId();
  const [ack, setAck] = useState(false);
  return (
    <div className="flex flex-col gap-4">
      <Callout tone="warning" title="Copy this signing secret now">
        It won't be shown again. Use it to verify the <code className="font-mono">X-Argus-Signature</code> header on each delivery.
        If you lose it, rotate the secret.
      </Callout>
      <div className="flex items-end gap-2">
        <TextField label={`Signing secret for ${webhookHost(hook.url)}`} value={hook.secret} readOnly spellCheck={false}
          className="flex-1 min-w-0 [&_input]:font-mono [&_input]:text-sm" onFocus={(e) => e.currentTarget.select()} />
        <CopyButton text={hook.secret} label="Copy signing secret" />
      </div>
      <label htmlFor={ackId} className="flex items-start gap-2.5 rounded-md border border-border-strong p-3 cursor-pointer has-[:checked]:border-accent has-[:checked]:bg-accent-subtle">
        <input id={ackId} type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)}
          className="mt-0.5 size-4 accent-[rgb(var(--accent))]" />
        <span className="text-sm font-medium">
          I've stored it
          <span className="block text-xs font-normal text-fg-muted">The secret is saved where your receiver can read it.</span>
        </span>
      </label>
      <div className="flex justify-end">
        <Button variant="primary" disabled={!ack} onClick={onDone}>Done</Button>
      </div>
    </div>
  );
}

/** Dialog wrapper that refuses to close while an unacknowledged secret is on screen. */
export function SecretDialog({ hook, title, onClose }: { hook: WebhookWithSecret | null; title: string; onClose: () => void }) {
  const [nudge, setNudge] = useState(false);
  const done = () => {
    setNudge(false);
    onClose();
  };
  return (
    <Dialog open={Boolean(hook)} wide title={title} onOpenChange={(o) => { if (!o) setNudge(true); }}>
      {hook && (
        <div className="flex flex-col gap-3">
          {nudge && (
            <p role="alert" className="flex items-center gap-2 text-sm text-danger">
              <TriangleAlert className="size-4 shrink-0" aria-hidden />
              Confirm you've stored the secret before closing. It can't be shown again.
            </p>
          )}
          <SecretReveal hook={hook} onDone={done} />
        </div>
      )}
    </Dialog>
  );
}

// ------------------------------------------------------------------ create
function CreateForm({ onCreated, onCancel }: { onCreated: (h: WebhookWithSecret) => void; onCancel: () => void }) {
  const create = useCreateWebhook();
  const eventsQ = useWebhookEvents();
  const [url, setUrl] = useState("");
  const [description, setDescription] = useState("");
  const [events, setEvents] = useState<string[]>([]);
  const [touched, setTouched] = useState(false);

  const errors: { url?: string; events?: string } = {};
  if (!/^https?:\/\/\S+$/i.test(url.trim())) errors.url = "Enter a URL starting with https:// (or http:// in development)";
  if (!events.length) errors.events = "Pick at least one event";
  const show = (k: keyof typeof errors) => (touched ? errors[k] : undefined);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setTouched(true);
    if (Object.keys(errors).length) return;
    try {
      onCreated(await create.mutateAsync({ url: url.trim(), events, description: description.trim(), enabled: true }));
    } catch {
      /* shown below */
    }
  };
  const toggle = (ev: string, on: boolean) => setEvents((cur) => (on ? [...cur, ev] : cur.filter((x) => x !== ev)));
  const eventsErrId = useId();

  return (
    <form onSubmit={submit} noValidate className="flex flex-col gap-4">
      <TextField label="Payload URL" type="url" inputMode="url" value={url} autoFocus spellCheck={false}
        placeholder="https://example.com/hooks/argus" error={show("url")} onChange={(e) => setUrl(e.target.value)} />
      <fieldset className="flex flex-col gap-2" aria-describedby={show("events") ? eventsErrId : undefined}>
        <legend className="text-sm font-medium mb-1">Events</legend>
        {eventsQ.isLoading ? (
          <Skeleton className="h-16" />
        ) : eventsQ.error ? (
          <p role="alert" className="text-sm text-danger">Could not load events: {errorMessage(eventsQ.error)}</p>
        ) : (
          <div className="grid gap-2 sm:grid-cols-2">
            {(eventsQ.data ?? []).map((ev) => (
              <label key={ev} className="flex items-center gap-2 rounded-md border border-border-strong px-3 h-9 cursor-pointer has-[:checked]:border-accent has-[:checked]:bg-accent-subtle">
                <input type="checkbox" checked={events.includes(ev)} onChange={(e) => toggle(ev, e.target.checked)}
                  className="size-4 accent-[rgb(var(--accent))]" />
                <span className="font-mono text-xs">{ev}</span>
              </label>
            ))}
          </div>
        )}
        {show("events") && <p id={eventsErrId} role="alert" className="text-xs text-danger">{errors.events}</p>}
      </fieldset>
      <TextField label="Description (optional)" value={description} maxLength={200}
        placeholder="e.g. Slack relay for failed runs" onChange={(e) => setDescription(e.target.value)} />
      {create.error && <Callout tone="danger" title="Could not create the webhook">{errorMessage(create.error)}</Callout>}
      <div className="flex justify-end gap-2 mt-1">
        <Button type="button" variant="ghost" onClick={onCancel}>Cancel</Button>
        <Button type="submit" variant="primary" icon={WebhookIcon} loading={create.isPending}>Create webhook</Button>
      </div>
    </form>
  );
}

export function CreateWebhookDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (o: boolean) => void }) {
  const [created, setCreated] = useState<WebhookWithSecret | null>(null);
  return (
    <>
      <Dialog open={open && !created} onOpenChange={onOpenChange} title="Add webhook"
        description="Argus POSTs signed JSON to this URL when the selected events happen.">
        <CreateForm onCreated={setCreated} onCancel={() => onOpenChange(false)} />
      </Dialog>
      <SecretDialog hook={created} title="Webhook created" onClose={() => { setCreated(null); onOpenChange(false); }} />
    </>
  );
}

// ------------------------------------------------------------------ typed delete
/** Same shape and wording as connections' ConfirmDelete, plus a typed-name gate on the destructive button. */
export function TypedConfirmDelete({ open, onOpenChange, kind, name, consequence, onConfirm, pending, error }: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  kind: string;
  name: string;
  consequence?: ReactNode;
  onConfirm: () => void;
  pending?: boolean;
  error?: unknown;
}) {
  const [typed, setTyped] = useState("");
  const match = typed.trim() === name;
  return (
    <Dialog open={open} onOpenChange={(o) => { if (!o) setTyped(""); onOpenChange(o); }} title={`Delete ${kind} “${name}”?`}
      description="This cannot be undone."
      footer={
        <>
          <Button onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button variant="danger" icon={Trash2} loading={pending} disabled={!match} onClick={onConfirm}>
            Delete {name}
          </Button>
        </>
      }>
      {consequence && <div className="text-sm text-fg-muted mb-3">{consequence}</div>}
      <TextField label={`Type ${name} to confirm`} value={typed} autoComplete="off" spellCheck={false}
        onChange={(e) => setTyped(e.target.value)} />
      {error != null && (
        <div className="mt-3">
          <Callout tone="danger" title={`Could not delete ${name}`}>{errorMessage(error)}</Callout>
        </div>
      )}
    </Dialog>
  );
}

// ------------------------------------------------------------------ deliveries drawer
export function DeliveryOutcome({ ok, label }: { ok: boolean; label?: ReactNode }) {
  return (
    <Badge tone={ok ? "success" : "danger"} className="rounded-full px-2">
      {ok ? <CheckCircle2 className="size-3" aria-hidden /> : <XCircle className="size-3" aria-hidden />}
      {label ?? (ok ? "OK" : "Failed")}
    </Badge>
  );
}

export function DeliveriesDrawer({ hook, onClose }: { hook: Webhook | null; onClose: () => void }) {
  const q = useWebhookDeliveries(hook?.id ?? null);
  const rows = q.data ?? [];
  return (
    <RDialog.Root open={Boolean(hook)} onOpenChange={(o) => !o && onClose()}>
      <RDialog.Portal>
        <RDialog.Overlay className="fixed inset-0 bg-black/40 z-dialog" />
        <RDialog.Content className="fixed inset-y-0 right-0 z-dialog w-full max-w-2xl bg-surface border-l border-border shadow-3 flex flex-col">
          <div className="flex items-start justify-between gap-4 p-4 border-b border-border">
            <div className="min-w-0">
              <RDialog.Title className="text-lg">Recent deliveries</RDialog.Title>
              <RDialog.Description className="text-sm text-fg-muted font-mono truncate">{hook?.url}</RDialog.Description>
            </div>
            <div className="flex items-center gap-1">
              <IconButton label="Refresh deliveries" icon={RefreshCw} loading={q.isFetching} onClick={() => void q.refetch()} />
              <RDialog.Close asChild><IconButton label="Close" icon={X} /></RDialog.Close>
            </div>
          </div>
          <div className="flex-1 overflow-auto p-4">
            {q.isLoading ? (
              <div role="status" aria-label="Loading deliveries" className="space-y-2">
                <Skeleton className="h-8" /><Skeleton className="h-8" /><Skeleton className="h-8" />
              </div>
            ) : q.error ? (
              <ErrorState error={q.error} onRetry={() => void q.refetch()} />
            ) : rows.length === 0 ? (
              <EmptyState icon={History} title="No deliveries yet" body="Send a test event or wait for a subscribed event to fire." />
            ) : (
              <div className="relative overflow-x-auto">
                <table className="w-full text-sm">
                  <caption className="sr-only">Last {rows.length} delivery attempts</caption>
                  <thead>
                    <tr className="text-left text-xs text-fg-subtle border-b border-border">
                      <th scope="col" className="py-2 pr-3 font-medium">Time</th>
                      <th scope="col" className="py-2 pr-3 font-medium">Event</th>
                      <th scope="col" className="py-2 pr-3 font-medium">Attempt</th>
                      <th scope="col" className="py-2 pr-3 font-medium">Result</th>
                      <th scope="col" className="py-2 pr-3 font-medium text-right">Duration</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-border">
                    {rows.map((d) => (
                      <tr key={d.id} className="align-top">
                        <td className="py-2 pr-3 whitespace-nowrap tabular">
                          <time dateTime={d.ts} title={fullDate(d.ts)}>{relTime(d.ts)}</time>
                        </td>
                        <td className="py-2 pr-3 font-mono text-xs">{d.event}</td>
                        <td className="py-2 pr-3 tabular">{d.attempt}</td>
                        <td className="py-2 pr-3">
                          <div className="flex flex-col gap-0.5">
                            <span className="inline-flex items-center gap-1.5">
                              <DeliveryOutcome ok={d.ok} />
                              <span className="tabular text-xs text-fg-muted">{d.status_code != null ? `HTTP ${d.status_code}` : "No response"}</span>
                            </span>
                            {d.error && <span className="text-xs text-danger break-words">{d.error}</span>}
                          </div>
                        </td>
                        <td className={cn("py-2 pr-3 text-right tabular whitespace-nowrap", d.duration_ms == null && "text-fg-subtle")}>
                          {d.duration_ms != null ? `${d.duration_ms} ms` : "—"}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </RDialog.Content>
      </RDialog.Portal>
    </RDialog.Root>
  );
}
