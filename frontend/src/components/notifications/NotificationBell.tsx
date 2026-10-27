// Header bell: unread badge (polled), popover inbox with links to runs, and an aria-live
// announcement when new notifications arrive.

import * as Popover from "@radix-ui/react-popover";
import { useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Bell, CheckCheck, CheckCircle2, ShieldAlert, XCircle } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Link } from "react-router";

import { Badge, Button, EmptyState, ErrorState, Skeleton } from "@/components/ui";
import {
  notificationKeys,
  useMarkAllRead,
  useMarkRead,
  useNotifications,
  useUnreadCount,
  type AppNotification,
} from "@/features/notifications/queries";
import { cn } from "@/lib/cn";
import { relTime } from "@/lib/format";

const ICONS = {
  "run.completed": { icon: CheckCircle2, cls: "text-success" },
  "run.failed": { icon: XCircle, cls: "text-danger" },
  "run.halted": { icon: ShieldAlert, cls: "text-danger" },
  "approval.requested": { icon: AlertTriangle, cls: "text-warning" },
} as const;

function Item({ n, onOpen }: { n: AppNotification; onOpen: () => void }) {
  const markRead = useMarkRead();
  const approval = n.kind === "approval.requested";
  const meta = ICONS[n.kind as keyof typeof ICONS] ?? ICONS["run.completed"];
  const unread = !n.read_at;
  const open = () => {
    if (unread) markRead.mutate(n.id);
    onOpen();
  };
  const content = (
    <>
      <span className="flex items-center gap-1.5">
        <span className={cn("text-sm truncate", unread && "font-semibold")}>{n.title}</span>
      </span>
      {n.body && <span className="block text-xs text-fg-muted line-clamp-2">{n.body}</span>}
      <span className="block text-2xs text-fg-subtle mt-0.5">{relTime(n.created_at)}</span>
    </>
  );
  return (
    <li data-kind={n.kind} data-unread={unread || undefined}
      className={cn("flex items-start gap-2 px-3 py-2.5", approval && unread && "bg-warning-subtle border-l-2 border-warning")}>
      <meta.icon className={cn("size-4 mt-0.5 shrink-0", meta.cls)} aria-hidden />
      <div className="min-w-0 flex-1">
        {approval && <Badge tone="warning" className="mb-1">Approval needed</Badge>}
        {n.run_id ? (
          <Link to={`/runs/${n.run_id}`} onClick={open} className="block rounded hover:underline focus-visible:outline-2 focus-visible:outline-accent">
            {content}
          </Link>
        ) : <div>{content}</div>}
      </div>
      {unread ? (
        <button type="button" onClick={() => markRead.mutate(n.id)} aria-label={`Mark “${n.title}” as read`}
          className="shrink-0 size-6 inline-flex items-center justify-center rounded hover:bg-surface-2 text-fg-subtle">
          <span className="size-2 rounded-full bg-accent" aria-hidden />
        </button>
      ) : <span className="sr-only">Read</span>}
    </li>
  );
}

export function NotificationBell() {
  const qc = useQueryClient();
  const count = useUnreadCount();
  const list = useNotifications();
  const markAll = useMarkAllRead();
  const [open, setOpen] = useState(false);
  const [announcement, setAnnouncement] = useState("");
  const unread = count.data ?? 0;

  // A rising unread count means new rows: refresh the list so they can be announced.
  const prevCount = useRef<number | undefined>(undefined);
  useEffect(() => {
    if (count.data === undefined) return;
    if (prevCount.current !== undefined && count.data > prevCount.current) {
      void qc.invalidateQueries({ queryKey: notificationKeys.list });
    }
    prevCount.current = count.data;
  }, [count.data, qc]);

  // Announce unread items not seen before (never on the first load).
  const seen = useRef<Set<string> | null>(null);
  useEffect(() => {
    if (!list.data) return;
    const fresh = list.data.filter((n) => !n.read_at && !seen.current?.has(n.id));
    if (seen.current && fresh.length) {
      const approvals = fresh.filter((n) => n.kind === "approval.requested").length;
      setAnnouncement(
        fresh.length === 1
          ? `New notification: ${fresh[0]!.title}`
          : `${fresh.length} new notifications${approvals ? `, ${approvals} need approval` : ""}`,
      );
    }
    seen.current = new Set([...(seen.current ?? []), ...list.data.map((n) => n.id)]);
  }, [list.data]);

  useEffect(() => {
    if (open) void list.refetch();
  }, [open]); // eslint-disable-line react-hooks/exhaustive-deps

  const label = unread ? `Notifications, ${unread} unread` : "Notifications";
  return (
    <>
      <div role="status" aria-live="polite" className="sr-only" data-testid="notification-announcer">{announcement}</div>
      <Popover.Root open={open} onOpenChange={setOpen}>
        <Popover.Trigger asChild>
          <button type="button" aria-label={label} title={label}
            className="relative size-8 inline-flex items-center justify-center rounded-md text-fg-muted hover:bg-surface-2 hover:text-fg">
            <Bell className="size-4" aria-hidden />
            {unread > 0 && (
              <span aria-hidden data-testid="notification-badge"
                className="absolute -top-0.5 -right-0.5 min-w-4 h-4 px-1 rounded-full bg-danger text-danger-fg text-2xs font-semibold leading-4 text-center tabular">
                {unread > 9 ? "9+" : unread}
              </span>
            )}
          </button>
        </Popover.Trigger>
        <Popover.Portal>
          <Popover.Content align="end" sideOffset={6} aria-label="Notifications"
            className="z-popover w-[min(24rem,calc(100vw-1rem))] rounded-lg border border-border bg-surface shadow-2 flex flex-col max-h-[70vh]">
            <div className="flex items-center gap-2 px-3 h-10 border-b border-border">
              <h2 className="text-sm font-semibold">Notifications</h2>
              <Button size="sm" variant="ghost" icon={CheckCheck} className="ml-auto" disabled={!unread}
                loading={markAll.isPending} onClick={() => markAll.mutate()}>
                Mark all read
              </Button>
            </div>
            <div className="overflow-auto">
              {list.isLoading ? (
                <div className="p-3 space-y-2"><Skeleton className="h-10" /><Skeleton className="h-10" /></div>
              ) : list.error ? (
                <div className="p-3"><ErrorState error={list.error} onRetry={() => void list.refetch()} /></div>
              ) : list.data?.length ? (
                <ul className="divide-y divide-border" aria-label="Recent notifications">
                  {list.data.map((n) => <Item key={n.id} n={n} onOpen={() => setOpen(false)} />)}
                </ul>
              ) : (
                <EmptyState icon={Bell} title="You're all caught up" body="Finished runs and approval requests show up here." />
              )}
            </div>
          </Popover.Content>
        </Popover.Portal>
      </Popover.Root>
    </>
  );
}
