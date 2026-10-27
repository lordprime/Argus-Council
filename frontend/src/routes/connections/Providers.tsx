import {
  CheckCircle2,
  ChevronDown,
  CircleDashed,
  Cloud,
  Hourglass,
  Cpu,
  KeyRound,
  Pencil,
  Plus,
  PowerOff,
  RefreshCw,
  Server,
  ShieldAlert,
  Trash2,
  TriangleAlert,
  Unplug,
  XCircle,
  Zap,
  type LucideIcon,
} from "lucide-react";
import { useMemo, useState, type FormEvent } from "react";

import { ConnectionsPage } from "@/components/connections/ConnectionsNav";
import { ConfirmDelete } from "@/components/connections/ConfirmDelete";
import { CardSkeletons, Chips, Meta, SectionTitle, Switch } from "@/components/connections/parts";
import { SecretField, secretPatch, type SecretAction } from "@/components/connections/SecretField";
import {
  Badge,
  Button,
  Callout,
  Card,
  Dialog,
  EmptyState,
  ErrorState,
  IconButton,
  PageHeader,
  SelectField,
  Skeleton,
  TextField,
  Toggle,
} from "@/components/ui";
import {
  useCreateProvider,
  useDeleteProvider,
  useProviderModels,
  useTestProvider,
  useUpdateProvider,
  type HealthStatus,
  type ProviderWithHealth,
  type TestResult,
} from "@/features/connections/queries";
import { errorMessage } from "@/lib/api";
import { relTime } from "@/lib/format";
import { useProviderKinds, useProviders } from "@/lib/queries";
import type { ProviderConfig, ProviderKind } from "@/lib/types";
import { toast } from "@/stores/toast";

const NAME_RE = /^[a-z0-9][a-z0-9_-]*$/;

type Tone = "neutral" | "success" | "warning" | "danger" | "info";
type StatusInfo = { tone: Tone; label: string; icon: LucideIcon };
const HEALTH: Record<Exclude<HealthStatus, "unknown">, StatusInfo> = {
  ok: { tone: "success", label: "Connected", icon: CheckCircle2 },
  unreachable: { tone: "danger", label: "Unreachable", icon: Unplug },
  auth_failed: { tone: "danger", label: "Auth failed", icon: ShieldAlert },
  rate_limited: { tone: "warning", label: "Rate limited", icon: Hourglass },
  error: { tone: "danger", label: "Error", icon: TriangleAlert },
};

/** Persisted health (from the server) wins; a just-run test on an older server fills in until then. */
function providerStatus(p: ProviderWithHealth, kind: ProviderKind | undefined, test?: TestResult): StatusInfo {
  if (!p.enabled) return { tone: "neutral", label: "Disabled", icon: PowerOff };
  if (kind?.requires_api_key && !p.has_api_key) return { tone: "warning", label: "Missing key", icon: KeyRound };
  if (p.health_status && p.health_status !== "unknown" && HEALTH[p.health_status]) return HEALTH[p.health_status];
  if (test?.ok) return { tone: "success", label: "Connected", icon: CheckCircle2 };
  if (test && /\b429\b|rate.?limit/i.test(test.text)) return { tone: "warning", label: "Rate limited", icon: XCircle };
  if (test) return { tone: "danger", label: "Unreachable", icon: XCircle };
  return { tone: "neutral", label: "Not tested", icon: CircleDashed };
}

// ------------------------------------------------------------------ dialog (create + edit)
function ProviderDialog({ open, onOpenChange, kinds, existing, initialKind, takenNames }: {
  open: boolean;
  onOpenChange: (o: boolean) => void;
  kinds: ProviderKind[];
  existing?: ProviderConfig;
  initialKind?: string;
  takenNames: string[];
}) {
  const create = useCreateProvider();
  const update = useUpdateProvider();
  const startKind = existing?.kind ?? initialKind ?? kinds[0]?.key ?? "";
  const [kindKey, setKindKey] = useState(startKind);
  const [name, setName] = useState(existing?.name ?? suggestName(startKind, takenNames));
  const [baseUrl, setBaseUrl] = useState(existing?.base_url ?? "");
  const [model, setModel] = useState(existing?.default_model ?? "");
  const [enabled, setEnabled] = useState(existing?.enabled ?? true);
  const [secret, setSecret] = useState<SecretAction>(existing ? { kind: "keep" } : { kind: "replace", value: "" });
  const [touched, setTouched] = useState(false);
  const kind = kinds.find((k) => k.key === kindKey);
  const needsUrl = kind?.protocol === "openai" && !kind.default_base_url;
  const mutation = existing ? update : create;

  const errors: { name?: string; baseUrl?: string; key?: string } = {};
  if (!existing) {
    if (!NAME_RE.test(name)) errors.name = "Lowercase letters, digits, '-' or '_', starting with a letter or digit";
    else if (takenNames.includes(name)) errors.name = `You already have a provider named ${name}`;
  }
  if (needsUrl && !baseUrl.trim()) errors.baseUrl = "Required for this provider";
  if (baseUrl.trim() && !/^https?:\/\//i.test(baseUrl.trim())) errors.baseUrl = "Must start with http:// or https://";
  if (!existing && kind?.requires_api_key && secret.kind === "replace" && !secret.value.trim()) errors.key = "This provider needs an API key";
  const invalid = Object.keys(errors).length > 0;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setTouched(true);
    if (invalid) return;
    try {
      if (existing) {
        const key = secretPatch(secret);
        await update.mutateAsync({
          id: existing.id,
          patch: {
            base_url: baseUrl.trim() || null,
            default_model: model.trim() || null,
            enabled,
            ...(key !== undefined ? { api_key: key } : {}),
          },
        });
        toast({ tone: "success", title: `Saved ${existing.name}` });
      } else {
        await create.mutateAsync({
          name, kind: kindKey, enabled, options: {},
          base_url: baseUrl.trim() || null,
          default_model: model.trim() || null,
          api_key: secretPatch(secret) ?? null,
        });
        toast({ tone: "success", title: `Connected ${name}`, body: "Use Test connection to check it." });
      }
      onOpenChange(false);
    } catch {
      /* shown in the dialog */
    }
  };

  const hosted = kinds.filter((k) => !k.self_hosted);
  const selfHosted = kinds.filter((k) => k.self_hosted);
  const show = (k: keyof typeof errors) => (touched ? errors[k] : undefined);

  return (
    <Dialog open={open} onOpenChange={onOpenChange} title={existing ? `Edit ${existing.name}` : "Connect a provider"}
      description={existing ? `${kind?.label ?? existing.kind} provider` : "Keys are stored encrypted and are never shown again."}>
      <form onSubmit={submit} className="flex flex-col gap-4" noValidate>
        {touched && invalid && (
          <Callout tone="danger" title="Fix the highlighted fields">{Object.values(errors).join(" · ")}</Callout>
        )}
        {!existing && (
          <div className="grid gap-4 sm:grid-cols-2">
            <SelectField label="Provider" value={kindKey}
              onChange={(e) => { setKindKey(e.target.value); setName(suggestName(e.target.value, takenNames)); }}>
              <optgroup label="Hosted">{hosted.map((k) => <option key={k.key} value={k.key}>{k.label}</option>)}</optgroup>
              <optgroup label="Self-hosted">{selfHosted.map((k) => <option key={k.key} value={k.key}>{k.label}</option>)}</optgroup>
            </SelectField>
            <TextField label="Name" required value={name} onChange={(e) => setName(e.target.value)} maxLength={64}
              hint="How councils refer to it, e.g. anthropic or local-llama" error={show("name")} spellCheck={false} />
          </div>
        )}
        <TextField label={needsUrl ? "Base URL" : "Base URL (optional)"} type="url" value={baseUrl} inputMode="url"
          onChange={(e) => setBaseUrl(e.target.value)} placeholder={kind?.default_base_url ?? "https://…"}
          hint={kind?.default_base_url ? `Default: ${kind.default_base_url}` : "OpenAI-compatible endpoint, usually ending in /v1"}
          error={show("baseUrl")} spellCheck={false} />
        <SecretField label={kind?.requires_api_key ? "API key" : "API key (optional)"} stored={Boolean(existing?.has_api_key)}
          action={secret} onChange={setSecret} required={!existing && kind?.requires_api_key} clearable
          hint={show("key") ?? undefined} />
        <div>
          <TextField label="Default model (optional)" value={model} onChange={(e) => setModel(e.target.value)} list="provider-models"
            placeholder={kind?.suggested_models[0] ?? "model id"} spellCheck={false}
            hint="Used for Test connection and when a council seat does not pick a model." />
          <datalist id="provider-models">{kind?.suggested_models.map((m) => <option key={m} value={m} />)}</datalist>
        </div>
        <Toggle label="Enabled" checked={enabled} onChange={setEnabled} hint="Disabled providers are hidden from model pickers." />
        {mutation.error && <Callout tone="danger" title="Could not save">{errorMessage(mutation.error)}</Callout>}
        <div className="flex justify-end gap-2">
          <Button type="button" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button type="submit" variant="primary" loading={mutation.isPending}>{existing ? "Save" : "Connect"}</Button>
        </div>
      </form>
    </Dialog>
  );
}

function suggestName(kind: string, taken: string[]): string {
  const base = kind.replace(/[^a-z0-9_-]/g, "-") || "provider";
  if (!taken.includes(base)) return base;
  for (let i = 2; ; i++) if (!taken.includes(`${base}-${i}`)) return `${base}-${i}`;
}

// ------------------------------------------------------------------ provider card
function ModelList({ name }: { name: string }) {
  const q = useProviderModels(name, true);
  if (q.isLoading) return <div className="flex gap-1"><Skeleton className="h-6 w-24" /><Skeleton className="h-6 w-32" /><Skeleton className="h-6 w-20" /></div>;
  if (q.error) return <p className="text-sm text-danger" role="alert">Could not list models: {errorMessage(q.error)}</p>;
  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center gap-2">
        <span className="text-xs text-fg-subtle">{q.data?.length ?? 0} models</span>
        <Button size="sm" variant="ghost" icon={RefreshCw} loading={q.isFetching} onClick={() => void q.refetch()}>Refresh</Button>
      </div>
      <Chips items={q.data ?? []} label={`Models for ${name}`} empty="The provider returned no models." className="max-h-40 overflow-auto" />
    </div>
  );
}

function HealthLine({ p }: { p: ProviderWithHealth }) {
  if (!p.health_checked_at || !p.health_status || p.health_status === "unknown") return null;
  const bad = p.health_status !== "ok";
  return (
    <div className="text-xs text-fg-muted">
      <p>
        Checked <time dateTime={p.health_checked_at} title={new Date(p.health_checked_at).toLocaleString()}>{relTime(p.health_checked_at)}</time>
        {p.health_latency_ms != null && <span className="tabular"> · {Math.round(p.health_latency_ms)} ms</span>}
      </p>
      {bad && p.health_detail && <p className={p.health_status === "rate_limited" ? "text-warning break-words" : "text-danger break-words"}>{p.health_detail}</p>}
    </div>
  );
}

function ProviderCard({ p, kind, onEdit, onDelete }: { p: ProviderWithHealth; kind?: ProviderKind; onEdit: () => void; onDelete: () => void }) {
  const test = useTestProvider();
  const update = useUpdateProvider();
  const [showModels, setShowModels] = useState(false);
  const status = providerStatus(p, kind, test.data);
  const testModel = p.default_model || kind?.suggested_models[0] || "";
  const headingId = `prov-${p.id}`;
  const Icon = kind?.self_hosted ? Server : Cloud;

  const toggle = (enabled: boolean) =>
    update.mutate({ id: p.id, patch: { enabled } }, {
      onError: (e) => toast({ tone: "danger", title: `Could not update ${p.name}`, body: errorMessage(e) }),
    });

  return (
    <Card as="article" aria-labelledby={headingId} className="p-4 flex flex-col gap-3">
      <div className="flex items-start gap-3">
        <span className="grid place-items-center size-9 rounded-md bg-accent-subtle text-accent shrink-0"><Icon className="size-5" aria-hidden /></span>
        <div className="min-w-0 flex-1">
          <h3 id={headingId} className="font-semibold truncate">{p.name}</h3>
          <p className="text-xs text-fg-subtle">{kind?.label ?? p.kind}{kind?.self_hosted && !/self-hosted/i.test(kind.label) ? " · self-hosted" : ""}</p>
        </div>
        <Badge tone={status.tone} className="rounded-full px-2 shrink-0"><status.icon className="size-3" aria-hidden />{status.label}</Badge>
      </div>
      <dl className="grid gap-3 sm:grid-cols-3">
        <Meta label="Base URL" mono>{p.base_url || kind?.default_base_url || "Default"}</Meta>
        <Meta label="Default model" mono>{p.default_model || "—"}</Meta>
        <Meta label="API key">
          {p.has_api_key ? (
            p.key_hint ? (
              <span className="inline-flex items-center gap-1" title="Stored key (masked)">
                <KeyRound className="size-3.5" aria-hidden /><span className="sr-only">Stored key </span>
                <code className="font-mono text-xs">{p.key_hint}</code>
              </span>
            ) : (
              <span className="inline-flex items-center gap-1"><KeyRound className="size-3.5" aria-hidden />Stored · write-only</span>
            )
          ) : kind?.requires_api_key ? <span className="text-warning">Not set</span> : "Not required"}
        </Meta>
      </dl>
      <HealthLine p={p} />
      {test.data && (
        <Callout tone={test.data.ok ? "success" : "danger"}
          title={test.data.ok ? `Connected · ${test.data.latencyMs} ms` : `Test failed after ${test.data.latencyMs} ms`}>
          {test.data.source !== "server" && <span className="font-mono text-xs">{test.data.model}</span>}
          {test.data.text && (
            <span className="block break-words mt-0.5">
              {test.data.ok && test.data.source !== "server" ? `“${test.data.text}”` : test.data.text}
            </span>
          )}
        </Callout>
      )}
      <div>
        <button type="button" aria-expanded={showModels} aria-controls={`${headingId}-models`} onClick={() => setShowModels((s) => !s)}
          className="inline-flex items-center gap-1 text-sm text-accent min-h-6">
          <ChevronDown className={`size-4 transition-transform duration-fast ${showModels ? "rotate-180" : ""}`} aria-hidden />
          {showModels ? "Hide models" : "Show models"}
        </button>
        <div id={`${headingId}-models`} className="mt-2" hidden={!showModels}>{showModels && <ModelList name={p.name} />}</div>
      </div>
      <div className="flex flex-wrap items-center gap-2 pt-3 border-t border-border">
        <Switch object={p.name} checked={p.enabled} onChange={toggle} disabled={update.isPending} />
        <div className="flex-1" />
        <Button size="sm" icon={Zap} loading={test.isPending} disabled={!testModel || !p.enabled}
          title={!testModel ? "Set a default model to test" : undefined}
          onClick={() => test.mutate({ provider: p.name, model: testModel })}>
          Test connection
        </Button>
        <IconButton label={`Edit ${p.name}`} icon={Pencil} onClick={onEdit} />
        <IconButton label={`Delete ${p.name}`} icon={Trash2} variant="danger-ghost" onClick={onDelete} />
      </div>
    </Card>
  );
}

// ------------------------------------------------------------------ page
export default function Providers() {
  const providers = useProviders();
  const kindsQ = useProviderKinds();
  const del = useDeleteProvider();
  const [dialog, setDialog] = useState<{ kind?: string; edit?: ProviderConfig } | null>(null);
  const [deleting, setDeleting] = useState<ProviderConfig | null>(null);
  const kinds = useMemo(() => kindsQ.data?.kinds ?? [], [kindsQ.data]);
  const kindOf = (k: string) => kinds.find((x) => x.key === k);
  const taken = providers.data?.map((p) => p.name) ?? [];
  const loading = providers.isLoading || kindsQ.isLoading;
  const error = providers.error ?? kindsQ.error;
  const system = kindsQ.data?.system_providers ?? [];

  return (
    <ConnectionsPage>
      <PageHeader title="LLM providers"
        description="Connect hosted APIs or self-hosted models. Keys are encrypted at rest and never shown again."
        actions={<Button variant="primary" icon={Plus} disabled={!kinds.length} onClick={() => setDialog({})}>Connect provider</Button>} />

      {loading ? (
        <CardSkeletons count={2} />
      ) : error ? (
        <Card><ErrorState error={error} onRetry={() => { void providers.refetch(); void kindsQ.refetch(); }} /></Card>
      ) : (
        <div className="flex flex-col gap-8">
          <section aria-labelledby="yours-h">
            <SectionTitle id="yours-h">Your providers</SectionTitle>
            {providers.data?.length ? (
              <div className="grid gap-4 lg:grid-cols-2 items-start">
                {providers.data.map((p) => (
                  <ProviderCard key={p.id} p={p} kind={kindOf(p.kind)} onEdit={() => setDialog({ edit: p })} onDelete={() => { del.reset(); setDeleting(p); }} />
                ))}
              </div>
            ) : (
              <Card as="div">
                <EmptyState icon={Cpu} title="No providers connected"
                  body="Councils use the offline demo model until you connect Anthropic, OpenAI, Gemini, Ollama or another provider."
                  action={<Button variant="primary" icon={Plus} onClick={() => setDialog({})}>Connect provider</Button>} />
              </Card>
            )}
          </section>

          {system.length > 0 && (
            <section aria-labelledby="system-h">
              <SectionTitle id="system-h" description="Set by the server's environment. Read-only here.">Configured via environment</SectionTitle>
              <ul className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
                {system.map((s) => (
                  <li key={s} className="flex items-center gap-3 rounded-lg border border-dashed border-border-strong bg-surface p-3">
                    <Server className="size-4 text-fg-subtle" aria-hidden />
                    <div className="min-w-0 flex-1">
                      <p className="font-medium truncate">{s === "mock" ? "Offline demo (mock)" : kindOf(s)?.label ?? s}</p>
                      <p className="text-xs text-fg-subtle font-mono">{s}</p>
                    </div>
                    <Badge tone="info">Environment</Badge>
                  </li>
                ))}
              </ul>
            </section>
          )}

          <section aria-labelledby="catalog-h">
            <SectionTitle id="catalog-h" description="Pick one to prefill the connection form.">Available providers</SectionTitle>
            <ul className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
              {kinds.map((k) => {
                const count = providers.data?.filter((p) => p.kind === k.key).length ?? 0;
                return (
                  <li key={k.key} className="flex items-center gap-3 rounded-lg border border-border bg-surface p-3">
                    <div className="min-w-0 flex-1">
                      <p className="font-medium truncate">{k.label}</p>
                      <p className="text-xs text-fg-subtle">
                        {k.self_hosted ? "Self-hosted" : "Hosted"} · {k.requires_api_key ? "API key" : "No key needed"}
                        {count > 0 && ` · ${count} connected`}
                      </p>
                    </div>
                    <Button size="sm" aria-label={`Connect ${k.label}`} onClick={() => setDialog({ kind: k.key })}>Connect</Button>
                  </li>
                );
              })}
            </ul>
          </section>
        </div>
      )}

      {dialog && (
        <ProviderDialog key={dialog.edit?.id ?? dialog.kind ?? "new"} open onOpenChange={(o) => !o && setDialog(null)}
          kinds={kinds} existing={dialog.edit} initialKind={dialog.kind} takenNames={taken} />
      )}
      {deleting && (
        <ConfirmDelete open onOpenChange={(o) => !o && setDeleting(null)} kind="provider" name={deleting.name}
          consequence={<>Council seats that use <strong>{deleting.name}</strong> will fail until you pick another provider. Its stored API key is destroyed.</>}
          pending={del.isPending} error={del.error}
          onConfirm={() => del.mutate(deleting.id, {
            onSuccess: () => { toast({ tone: "success", title: `Deleted ${deleting.name}` }); setDeleting(null); },
          })} />
      )}
    </ConnectionsPage>
  );
}
