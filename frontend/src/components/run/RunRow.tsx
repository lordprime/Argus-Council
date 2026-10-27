import { Link } from "react-router";

import type { Run } from "@/lib/types";

import { RunStackedBody, useCouncilNames } from "./RunCells";

/** Home's stacked run row, built from the same cells as the Runs table (audit C-14). */
export function RunRow({ run, names }: { run: Run; names?: Map<string, string> }) {
  // Callers that render many rows pass the names map; a lone row resolves its own (cached) lookup.
  const own = useCouncilNames(names ? undefined : [run]);
  return (
    <li>
      <Link to={`/runs/${run.id}`} className="flex items-center gap-3 px-4 py-3 hover:bg-surface-2 focus-visible:bg-surface-2">
        <RunStackedBody run={run} names={names ?? own} />
      </Link>
    </li>
  );
}

/** A list of stacked run rows that resolves council names once for the whole list. */
export function RunRowList({ runs }: { runs: Run[] }) {
  const names = useCouncilNames(runs);
  return <ul className="divide-y divide-border">{runs.map((r) => <RunRow key={r.id} run={r} names={names} />)}</ul>;
}
