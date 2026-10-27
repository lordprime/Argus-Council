// Admin-only user management (backend/argus/api/v1/admin.py).

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, V1 } from "@/lib/api";
import type { User } from "@/lib/types";
import { useSession } from "@/stores/session";

export type Role = "admin" | "member" | "viewer";
export const ROLES: { value: Role; label: string; hint: string }[] = [
  { value: "admin", label: "Admin", hint: "Full access, including user management" },
  { value: "member", label: "Member", hint: "Create and run councils, manage own connections" },
  { value: "viewer", label: "Viewer", hint: "Read-only access" },
];

export interface AdminUser {
  id: string;
  email: string;
  display_name: string;
  role: Role;
  is_admin: boolean;
  is_active: boolean;
  created_at: string;
  last_login_at?: string | null;
  run_count?: number;
}

export interface AdminUserPatch {
  role?: Role;
  is_active?: boolean;
}

/** /auth/me gains `role` with RBAC; `is_admin` remains as a mirror for older servers. */
export type SessionUser = User & { role?: Role };

export const isAdminUser = (u: SessionUser | null | undefined) => Boolean(u && (u.role === "admin" || u.is_admin));

/** Whether the signed-in user may see admin-only settings. */
export function useIsAdmin() {
  return isAdminUser(useSession((s) => s.user) as SessionUser | null);
}

export const adminKeys = { users: ["admin", "users"] as const };

export const useAdminUsers = (enabled = true) =>
  useQuery({
    queryKey: adminKeys.users,
    queryFn: () =>
      api.get<AdminUser[] | { users: AdminUser[] }>(`${V1}/admin/users`).then((r) => (Array.isArray(r) ? r : r.users)),
    enabled,
  });

export function useUpdateAdminUser() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, patch }: { id: string; patch: AdminUserPatch }) =>
      api.patch<AdminUser>(`${V1}/admin/users/${encodeURIComponent(id)}`, patch),
    onSuccess: (u) => qc.setQueryData<AdminUser[]>(adminKeys.users, (list) => list?.map((x) => (x.id === u.id ? { ...x, ...u } : x))),
  });
}
