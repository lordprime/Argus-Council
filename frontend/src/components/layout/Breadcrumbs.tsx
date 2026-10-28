// Top-bar breadcrumb (spec §2, audit C-11). Each route declares its trail in `handle.crumbs`, so detail
// pages no longer need their own back links: desktop shows the full trail, mobile a single
// "back to parent" link next to the logo.

import { ChevronLeft, ChevronRight } from "lucide-react";
import type { ReactNode } from "react";
import { Link, useLocation, useMatches, type Params } from "react-router";

import { cn } from "@/lib/cn";
import { useCouncil, useRunDetail } from "@/lib/queries";

export interface Crumb {
  label: ReactNode;
  /** Omit on the current page (the last crumb). */
  to?: string;
}

export interface RouteHandle {
  /** The trail for this route, root first. A trail of one item is not shown (the page h1 already says it). */
  crumbs?: (ctx: { params: Params<string>; search: URLSearchParams }) => Crumb[];
}

/** Live labels: they read the same cached queries the pages use, so they cost no extra request. */
export function RunTopicCrumb({ runId }: { runId: string }) {
  const run = useRunDetail(runId);
  return <>{run.data?.topic ?? "Run"}</>;
}

export function CouncilNameCrumb({ councilId }: { councilId: string }) {
  const council = useCouncil(councilId);
  return <>{council.data?.name ?? "Council"}</>;
}

/** The trail of the deepest matched route that declares one. */
export function useCrumbs(): Crumb[] {
  const matches = useMatches();
  const { search } = useLocation();
  for (let i = matches.length - 1; i >= 0; i--) {
    const m = matches[i]!;
    const handle = m.handle as RouteHandle | undefined;
    if (handle?.crumbs) return handle.crumbs({ params: m.params, search: new URLSearchParams(search) });
  }
  return [];
}

export function Breadcrumbs({ className }: { className?: string }) {
  const crumbs = useCrumbs();
  if (crumbs.length < 2) return null;
  const parent = [...crumbs.slice(0, -1)].reverse().find((c) => c.to);
  return (
    <div className={cn("min-w-0 flex items-center", className)}>
      <nav aria-label="Breadcrumb" className="hidden md:block min-w-0">
        <ol className="flex items-center gap-1 text-sm min-w-0">
          {crumbs.map((c, i) => {
            const last = i === crumbs.length - 1;
            return (
              <li key={i} className={cn("flex items-center gap-1", last ? "min-w-0 text-fg font-medium" : "shrink-0 text-fg-muted max-w-[16rem]")}>
                {i > 0 && <ChevronRight className="size-3.5 shrink-0 text-fg-subtle" aria-hidden />}
                {last || !c.to ? (
                  <span aria-current={last ? "page" : undefined} className="truncate">{c.label}</span>
                ) : (
                  <Link to={c.to} className="truncate rounded-sm hover:text-fg hover:underline underline-offset-2">{c.label}</Link>
                )}
              </li>
            );
          })}
        </ol>
      </nav>
      {parent?.to && (
        <Link to={parent.to}
          className={cn("md:hidden inline-flex items-center gap-0.5 min-w-0 h-8 pr-2 rounded-md text-sm text-fg-muted hover:text-fg",
            "[@media(pointer:coarse)]:min-h-11")}>
          <ChevronLeft className="size-4 shrink-0" aria-hidden />
          <span className="sr-only">Back to</span>{" "}
          <span className="truncate">{parent.label}</span>
        </Link>
      )}
    </div>
  );
}
