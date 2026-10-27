import { Search, Users as UsersIcon } from "lucide-react";
import { useMemo, useState } from "react";

import { Switch } from "@/components/connections/parts";
import { SettingsLayout } from "@/components/settings/SettingsNav";
import { Badge, Callout, Card, EmptyState, ErrorState, Skeleton } from "@/components/ui";
import { ROLES, useAdminUsers, useIsAdmin, useUpdateAdminUser, type AdminUser, type AdminUserPatch, type Role } from "@/features/admin/queries";
import { ApiError, errorMessage } from "@/lib/api";
import { relTime } from "@/lib/format";
import { useSession } from "@/stores/session";
import { toast } from "@/stores/toast";

const fullDate = (iso: string) => new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });

function rowError(err: unknown): string {
  if (err instanceof ApiError && err.status === 409) return err.message || "This change would leave the workspace without an active admin.";
  return errorMessage(err);
}

function UserRow({ u, self }: { u: AdminUser; self: boolean }) {
  const update = useUpdateAdminUser();
  const [error, setError] = useState<string | null>(null);
  const errId = `user-${u.id}-err`;

  const save = (patch: AdminUserPatch, what: string) => {
    setError(null);
    update.mutate({ id: u.id, patch }, {
      onSuccess: () => toast({ tone: "success", title: `Updated ${u.email}`, body: what }),
      onError: (e) => setError(rowError(e)),
    });
  };

  return (
    <tr className="align-top">
      <td className="py-3 pl-4 pr-3 min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-medium">{u.display_name || u.email}</span>
          {self && <Badge tone="accent">You</Badge>}
          {!u.is_active && <Badge tone="neutral">Deactivated</Badge>}
        </div>
        <p className="text-xs text-fg-muted break-all">{u.email}</p>
        {error && <p id={errId} role="alert" className="mt-1 text-xs text-danger">{error}</p>}
      </td>
      <td className="py-3 pr-3">
        <select aria-label={`Role for ${u.email}`} value={u.role} disabled={update.isPending}
          aria-invalid={error ? true : undefined} aria-describedby={error ? errId : undefined}
          onChange={(e) => save({ role: e.target.value as Role }, `Role set to ${e.target.value}`)}
          className="h-8 rounded-md border border-border-strong bg-surface px-2 text-sm text-fg disabled:opacity-60">
          {ROLES.map((r) => <option key={r.value} value={r.value} title={r.hint}>{r.label}</option>)}
        </select>
      </td>
      <td className="py-3 pr-3">
        <Switch label="Active" object={u.email} checked={u.is_active} disabled={update.isPending}
          onChange={(v) => save({ is_active: v }, v ? "Account reactivated" : "Account deactivated")} />
      </td>
      <td className="py-3 pr-3 hidden md:table-cell text-sm tabular whitespace-nowrap">
        {u.last_login_at
          ? <time dateTime={u.last_login_at} title={fullDate(u.last_login_at)}>{relTime(u.last_login_at)}</time>
          : <span className="text-fg-muted">Never</span>}
      </td>
      <td className="py-3 pr-4 hidden md:table-cell text-sm tabular whitespace-nowrap">
        <time dateTime={u.created_at} title={fullDate(u.created_at)}>{relTime(u.created_at)}</time>
      </td>
    </tr>
  );
}

function UsersTable() {
  const q = useAdminUsers();
  const me = useSession((s) => s.user);
  const [query, setQuery] = useState("");
  const users = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const all = q.data ?? [];
    return needle ? all.filter((u) => `${u.email} ${u.display_name} ${u.role}`.toLowerCase().includes(needle)) : all;
  }, [q.data, query]);

  if (q.isLoading) return <div role="status" aria-label="Loading users"><Card as="div" className="p-4 space-y-3"><Skeleton className="h-8" /><Skeleton className="h-8" /><Skeleton className="h-8" /></Card></div>;
  if (q.error) return <Card as="div"><ErrorState error={q.error} onRetry={() => void q.refetch()} /></Card>;

  return (
    <section aria-labelledby="users-h" className="flex flex-col gap-3">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <h2 id="users-h" className="text-lg">People <span className="text-fg-subtle font-normal tabular">({q.data?.length ?? 0})</span></h2>
        <div className="relative w-full sm:w-64">
          <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 size-4 text-fg-subtle" aria-hidden />
          <input type="search" aria-label="Search users" placeholder="Search name, email or role" value={query}
            onChange={(e) => setQuery(e.target.value)}
            className="h-9 w-full rounded-md border border-border-strong bg-surface pl-8 pr-3 text-sm text-fg placeholder:text-fg-subtle" />
        </div>
      </div>
      <Card as="div" className="overflow-x-auto">
        {users.length === 0 ? (
          <EmptyState icon={UsersIcon} title={query ? "No matching users" : "No users"} body={query ? "Try a different search." : undefined} />
        ) : (
          <table className="w-full text-sm">
            <caption className="sr-only">Users and their roles</caption>
            <thead>
              <tr className="text-left text-xs text-fg-subtle border-b border-border">
                <th scope="col" className="py-2 pl-4 pr-3 font-medium">User</th>
                <th scope="col" className="py-2 pr-3 font-medium">Role</th>
                <th scope="col" className="py-2 pr-3 font-medium">Status</th>
                <th scope="col" className="py-2 pr-3 font-medium hidden md:table-cell">Last sign-in</th>
                <th scope="col" className="py-2 pr-4 font-medium hidden md:table-cell">Joined</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {users.map((u) => <UserRow key={u.id} u={u} self={u.id === me?.id} />)}
            </tbody>
          </table>
        )}
      </Card>
      <p className="text-xs text-fg-subtle">
        Admins manage users and every setting. Members create and run councils. Viewers have read-only access.
        You can't demote or deactivate yourself or the last active admin.
      </p>
    </section>
  );
}

export default function Users() {
  const admin = useIsAdmin();
  return (
    <SettingsLayout title="Users" description="Manage who can sign in and what they can do.">
      {admin ? (
        <UsersTable />
      ) : (
        <Callout tone="warning" title="Admins only">
          Ask an admin if you need to change a role or deactivate an account.
        </Callout>
      )}
    </SettingsLayout>
  );
}
