import { useMutation, useQueryClient } from "@tanstack/react-query";

import { api, V1 } from "@/lib/api";
import type { AutoMode, Run, RunStatus } from "@/lib/types";

/** Mirrors backend TERMINAL_STATUSES: only these runs can be trashed or deleted. */
export const TERMINAL: ReadonlySet<RunStatus> = new Set<RunStatus>(["completed", "failed", "cancelled", "halted"]);

/** Which slice of the run list: GET /runs?view=. "all" is everything not in the trash. */
export type RunView = "active" | "archived" | "trash";

/** archive/unarchive keep a run intact; trash is a soft delete (purged after the retention period); delete is permanent. */
export type RunAction = "archive" | "unarchive" | "trash" | "restore" | "delete";

export interface BulkDeleteResult {
  deleted: string[];
  failed: { id: string; error: unknown }[];
}
export type BulkResult = BulkDeleteResult;

/** Applies one action to runs one by one (there is no bulk endpoint) and reports partial failures. */
export function useRunsAction() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({ ids, action }: { ids: string[]; action: RunAction }): Promise<BulkResult> => {
      const results = await Promise.allSettled(
        ids.map((id) => (action === "delete" ? api.del(`${V1}/runs/${id}`) : api.post(`${V1}/runs/${id}/${action}`))),
      );
      const out: BulkResult = { deleted: [], failed: [] };
      results.forEach((r, i) => {
        const id = ids[i]!;
        if (r.status === "fulfilled") out.deleted.push(id);
        else out.failed.push({ id, error: r.reason });
      });
      return out;
    },
    onSettled: () => qc.invalidateQueries({ queryKey: ["runs"] }),
  });
}

/** Permanent delete (from the trash). */
export function useDeleteRuns() {
  const action = useRunsAction();
  return { ...action, mutateAsync: (ids: string[]) => action.mutateAsync({ ids, action: "delete" }) };
}

export function useEmptyTrash() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api.post<{ deleted: number }>(`${V1}/runs/trash/empty`),
    onSettled: () => qc.invalidateQueries({ queryKey: ["runs"] }),
  });
}

/** Switch a live run's auto mode (POST /runs/{id}/auto-mode); a flag left out keeps its value. */
export function useSetAutoMode(runId: string) {
  return useMutation({
    mutationFn: (flags: Partial<AutoMode>) => api.post<AutoMode>(`${V1}/runs/${runId}/auto-mode`, flags),
  });
}

/** Runs that can be resumed where they stopped (POST /runs/{id}/continue). */
export const RESUMABLE: ReadonlySet<string> = new Set(["halted", "failed", "cancelled"]);

/**
 * Resume a halted, failed or cancelled run as a new run that replays the work already finished and
 * does only the rest. `guardrails` are merged over the old run's (for example a bigger budget).
 */
export function useResumeRun(runId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (guardrails: Record<string, unknown> = {}) => api.post<Run>(`${V1}/runs/${runId}/continue`, { guardrails }),
    onSettled: () => qc.invalidateQueries({ queryKey: ["runs"] }),
  });
}
