import { useMutation, useQueryClient } from "@tanstack/react-query";

import { api, V1 } from "@/lib/api";
import type { RunStatus } from "@/lib/types";

/** Mirrors backend TERMINAL_STATUSES: only these runs can be deleted. */
export const TERMINAL: ReadonlySet<RunStatus> = new Set<RunStatus>(["completed", "failed", "cancelled", "halted"]);

export interface BulkDeleteResult {
  deleted: string[];
  failed: { id: string; error: unknown }[];
}

/** Deletes runs one by one (there is no bulk endpoint) and reports partial failures. */
export function useDeleteRuns() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (ids: string[]): Promise<BulkDeleteResult> => {
      const results = await Promise.allSettled(ids.map((id) => api.del(`${V1}/runs/${id}`)));
      const out: BulkDeleteResult = { deleted: [], failed: [] };
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
