// In-app notifications (backend/argus/api/v1/notifications.py).

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, V1 } from "@/lib/api";

export type NotificationKind = "run.completed" | "run.failed" | "run.halted" | "approval.requested";

export interface AppNotification {
  id: string;
  kind: NotificationKind | string;
  title: string;
  body: string;
  run_id: string | null;
  data: { approval_id?: string; status?: string; mode?: string };
  read_at: string | null;
  created_at: string;
}

export const notificationKeys = {
  count: ["notifications", "count"] as const,
  list: ["notifications", "list"] as const,
};

/** Poll interval for the header badge. Paused while the tab is hidden. */
export const POLL_MS = 30_000;
const BASE = `${V1}/notifications`;

export const useUnreadCount = () =>
  useQuery({
    queryKey: notificationKeys.count,
    queryFn: () => api.get<{ unread: number }>(`${BASE}/unread-count`).then((r) => r.unread),
    refetchInterval: POLL_MS,
    refetchIntervalInBackground: false,
    refetchOnWindowFocus: true,
    staleTime: 10_000,
  });

export const useNotifications = (enabled = true) =>
  useQuery({
    queryKey: notificationKeys.list,
    queryFn: () => api.get<{ notifications: AppNotification[]; unread: number }>(`${BASE}?limit=30`).then((r) => r.notifications),
    enabled,
    staleTime: 10_000,
  });

function useInboxMutation<TVars>(fn: (v: TVars) => Promise<unknown>, optimistic: (list: AppNotification[], v: TVars) => AppNotification[]) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: fn,
    onMutate: (v: TVars) => {
      const prev = qc.getQueryData<AppNotification[]>(notificationKeys.list);
      if (prev) {
        const next = optimistic(prev, v);
        qc.setQueryData(notificationKeys.list, next);
        qc.setQueryData(notificationKeys.count, next.filter((n) => !n.read_at).length);
      }
      return { prev };
    },
    onError: (_e, _v, ctx) => {
      if (ctx?.prev) qc.setQueryData(notificationKeys.list, ctx.prev);
    },
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: ["notifications"] });
    },
  });
}

export const useMarkRead = () =>
  useInboxMutation(
    (id: string) => api.post(`${BASE}/${encodeURIComponent(id)}/read`),
    (list, id) => list.map((n) => (n.id === id && !n.read_at ? { ...n, read_at: new Date().toISOString() } : n)),
  );

export const useMarkAllRead = () =>
  useInboxMutation(
    (_: void) => api.post(`${BASE}/read-all`),
    (list) => list.map((n) => (n.read_at ? n : { ...n, read_at: new Date().toISOString() })),
  );
