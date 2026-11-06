"""Tools over the council knowledge graph (argus.runtime.graph): search, read and link nodes.

Every seat has them while the graph is on (they are registered with the graph), and they are
parallel-safe: reading the graph, or adding one edge to it, never conflicts with another call.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from argus.runtime.graph import NODE_TYPES, RELATIONS, graph_for
from argus.tools.base import StrList, Tool, ToolCallContext, ToolResult

_OFF = "The council's knowledge graph is turned off for this run (guardrails.graph.enabled)."


class GraphQuery(Tool):
    name = "graph_query"
    # Like the board: every seat may use it, whatever its role's tool list says.
    source = "council"
    parallel_safe = True
    read_only = True
    description = (
        "Search the council's knowledge graph: claims, evidence (sources), step and helper results, "
        "entities, decisions and open questions, from this run and the council's earlier runs. "
        "Returns ranked node summaries with ids and their neighbours (hops along edges). Use "
        "graph_read with an id for the full text."
    )

    class Args(BaseModel):
        query: str = Field(default="", max_length=500)
        types: StrList = Field(default_factory=list, max_length=6)
        hops: int = Field(default=1, ge=0, le=2)
        limit: int = Field(default=8, ge=1, le=30)

    async def run(self, args: Args, ctx: ToolCallContext) -> ToolResult:
        graph = graph_for(ctx.run)
        if graph is None:
            return ToolResult.error(_OFF)
        bad = [t for t in args.types if t not in NODE_TYPES]
        if bad:
            return ToolResult.error(
                f"Unknown node type(s): {', '.join(bad)}. Use any of: {', '.join(NODE_TYPES)}."
            )
        await graph.ready()
        text = graph.render_query(args.query, types=args.types, hops=args.hops, limit=args.limit)
        return ToolResult(text)


class GraphRead(Tool):
    name = "graph_read"
    source = "council"
    parallel_safe = True
    read_only = True
    description = (
        "Read one node of the council's knowledge graph in full (e.g. a step result [res_…], a "
        "claim [clm_…], a source [evd_…] or a board note [nte_…]) with all its edges."
    )

    class Args(BaseModel):
        node_id: str = Field(min_length=1, max_length=64)

    async def run(self, args: Args, ctx: ToolCallContext) -> ToolResult:
        graph = graph_for(ctx.run)
        if graph is None:
            return ToolResult.error(_OFF)
        await graph.ready()
        text = graph.read(args.node_id.strip().strip("[]\"'"))
        if text is None:
            return ToolResult.error(
                f"No node {args.node_id} in the council's graph. Find ids with graph_query first."
            )
        return ToolResult(text, data={"node_id": args.node_id})


class GraphLink(Tool):
    name = "graph_link"
    source = "council"
    parallel_safe = True
    description = (
        "Add an edge to the council's knowledge graph between two existing nodes, e.g. a claim "
        "that 'supports' or 'refutes' another, or a result that 'answers' a question."
    )

    class Args(BaseModel):
        model_config = ConfigDict(populate_by_name=True)

        from_: str = Field(alias="from", min_length=1, max_length=64)
        to: str = Field(min_length=1, max_length=64)
        relation: Literal[RELATIONS]  # type: ignore[valid-type]

    async def run(self, args: Args, ctx: ToolCallContext) -> ToolResult:
        graph = graph_for(ctx.run)
        if graph is None:
            return ToolResult.error(_OFF)
        await graph.ready()
        src, dst = args.from_.strip(), args.to.strip()
        missing = [k for k in (src, dst) if k not in graph.nodes]
        if missing:
            return ToolResult.error(
                f"No node {', '.join(missing)} in the council's graph. Find ids with graph_query first."
            )
        if src == dst:
            return ToolResult.error("A node cannot be linked to itself.")
        added = graph.link(src, dst, args.relation, ctx.member_name)
        verb = "Linked" if added else "Already linked:"
        return ToolResult(f"{verb} {src} {args.relation} {dst}.", data={"added": added})


def graph_tools() -> list[Tool]:
    return [GraphQuery(), GraphRead(), GraphLink()]
