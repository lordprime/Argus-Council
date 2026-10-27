// Outbound webhooks (backend/argus/api/v1/webhooks.py). Secrets are returned only by create and rotate.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, V1 } from "@/lib/api";

export interface Webhook {
  id: string;
  url: string;
  events: string[];
  description: string;
  enabled: boolean;
  failure_count: number;
  last_status: number | string | null;
  last_delivery_at: string | null;
  created_at: string;
}

export type WebhookWithSecret = Webhook & { secret: string };
export type WebhookTestResult = Webhook & { ok: boolean };

export interface WebhookDelivery {
  id: number;
  ts: string;
  event: string;
  delivery_id: string;
  attempt: number;
  status_code: number | null;
  ok: boolean;
  error: string | null;
  duration_ms: number | null;
}

export interface WebhookCreate {
  url: string;
  events: string[];
  description: string;
  enabled: boolean;
}

export const webhookKeys = {
  all: ["webhooks"] as const,
  events: ["webhookEvents"] as const,
  deliveries: (id: string) => ["webhookDeliveries", id] as const,
};

const enc = encodeURIComponent;
const BASE = `${V1}/webhooks`;

export const useWebhooks = () =>
  useQuery({ queryKey: webhookKeys.all, queryFn: () => api.get<{ webhooks: Webhook[] }>(BASE).then((r) => r.webhooks) });

export const useWebhookEvents = () =>
  useQuery({
    queryKey: webhookKeys.events,
    queryFn: () => api.get<{ events: string[] }>(`${BASE}/events`).then((r) => r.events),
    staleTime: Infinity,
  });

export const useWebhookDeliveries = (id: string | null) =>
  useQuery({
    queryKey: webhookKeys.deliveries(id ?? ""),
    queryFn: () => api.get<{ deliveries: WebhookDelivery[] }>(`${BASE}/${enc(id!)}/deliveries?limit=50`).then((r) => r.deliveries),
    enabled: Boolean(id),
  });

function useHookMutation<TVars, TData>(fn: (v: TVars) => Promise<TData>, deliveriesOf?: (v: TVars) => string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: fn,
    onSettled: (_d, _e, v) => {
      void qc.invalidateQueries({ queryKey: webhookKeys.all });
      if (deliveriesOf) void qc.invalidateQueries({ queryKey: webhookKeys.deliveries(deliveriesOf(v)) });
    },
  });
}

export const useCreateWebhook = () => useHookMutation((body: WebhookCreate) => api.post<WebhookWithSecret>(BASE, body));
export const useUpdateWebhook = () =>
  useHookMutation(({ id, patch }: { id: string; patch: Partial<WebhookCreate> }) => api.patch<Webhook>(`${BASE}/${enc(id)}`, patch));
export const useRotateWebhookSecret = () =>
  useHookMutation((id: string) => api.post<WebhookWithSecret>(`${BASE}/${enc(id)}/rotate-secret`));
export const useTestWebhook = () =>
  useHookMutation((id: string) => api.post<WebhookTestResult>(`${BASE}/${enc(id)}/test`), (id) => id);
export const useDeleteWebhook = () => useHookMutation((id: string) => api.del(`${BASE}/${enc(id)}`));

/** Short display name for a webhook: its host (falls back to the raw URL). */
export function webhookHost(url: string): string {
  try {
    return new URL(url).host || url;
  } catch {
    return url;
  }
}
