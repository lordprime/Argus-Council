"""The council knowledge graph: compact, structured context that outlives the run.

Each run keeps one ``CouncilGraph`` in memory (``ctx.extra["graph"]``) and writes it through to
``graph_nodes`` / ``graph_edges``, scoped like ``memory_items``: the run's owner plus its council,
or owner plus null for ad-hoc runs. Later runs of the same council load it back, so what one run
learned is there for the next.

The graph is built deterministically from the run's events (no model calls):

* step and helper results (``task.completed``) become ``result`` nodes
* citations (``citation.added``) become ``evidence`` nodes, linked by ``cites``
* board notes become ``result`` / ``decision`` / ``question`` nodes
* debate claims, challenges, responses and resolutions (``debate.*``) become ``claim`` and
  ``question`` nodes with ``supports`` / ``refutes`` / ``about`` / ``cites`` edges
* entities are capitalised and technical n-grams of claim statements, note titles and headings

It feeds the context members get: a short digest per dependency instead of its raw text
(``upstream_digest``), a compact board (``board_text``) and "What this council already knows"
(``recall_text``). Members pull detail with the ``graph_*`` tools (``argus.tools.graph``).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import re
import uuid
from collections import OrderedDict, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import or_, select, update

from argus.core.logging import get_logger
from argus.db.models import GraphEdge, GraphNode
from argus.knowledge.bm25 import Doc, rank
from argus.knowledge.text import STOPWORDS, term_counts, tokenize

if TYPE_CHECKING:
    from argus.runtime.context import RunContext
    from argus.runtime.events import Envelope
    from argus.tools.base import ToolRegistry

log = get_logger(__name__)

NODE_TYPES = ("claim", "evidence", "result", "entity", "decision", "question")
RELATIONS = ("supports", "refutes", "cites", "derived_from", "answers", "mentions", "about")
SUMMARY_CHARS = 300
BODY_CHARS = 100_000  # stored per node
READ_CHARS = 20_000  # graph_read returns at most this much of a body
UPSTREAM_CHARS = 24_000  # what _run_step pasted before the graph (the "raw" side of context.saved)
PRIOR_NODES = 2_000  # nodes of earlier runs loaded at the start of a run
PRIOR_EDGES = 8_000
ENTITIES_PER_TEXT = 8
LOAD_TIMEOUT_S = 10.0
THOUGHTS_KEPT = 48
# Tasks whose answer is a piece of the council's work (the others are talks about the work).
_RESULT_PHASES = frozenset({"execute", "synthesize", "compile", "a2a_delegate"})
_RESULT_PREFIXES = ("spawn", "helper")

POINTER = (
    "These are summaries from the council's knowledge graph, not the full work. Call graph_read with "
    "a node id (in brackets) for the full text, and graph_query to search what the council knows."
)

_SOURCE_REF = re.compile(r"\[Source (\d+)\]")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")
_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.+)$")
_WORD = r"[A-Z][a-zA-Z0-9]*(?:[-'][A-Za-z0-9]+)*"
_CAPS = re.compile(rf"\b{_WORD}(?:\s+(?:of\s+|de\s+|the\s+)?{_WORD}){{0,3}}")
_TECH = re.compile(
    r"\b(?:[A-Za-z]+[-_.]?\d[\w.-]*|[A-Z]{2,}[A-Z0-9]*s?|[a-z]+[A-Z]\w*|[A-Z][a-z]+[A-Z]\w*|\w+_\w+)\b"
)
_MD = re.compile(r"[*_`>]+|^#+\s*", re.M)
# Capitalised words that are just sentence furniture, never entities on their own.
_COMMON = frozenset(
    """also however therefore thus overall first second third finally next then note see yes no
    summary conclusion introduction background results result findings finding analysis key step
    source sources table figure option evidence claim claims question questions answer answers
    task tasks council report draft review verdict approve revise done""".split()
)


# ---------------------------------------------------------------- text helpers
def clean(text: str) -> str:
    """Markdown markers removed and whitespace collapsed."""
    return " ".join(_MD.sub("", text or "").split())


def summarize(text: str, limit: int = SUMMARY_CHARS) -> str:
    """A deterministic summary of at most ``limit`` chars: the leading sentences that fit."""
    flat = clean(text)
    if len(flat) <= limit:
        return flat
    cut = flat[: limit - 1]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if end >= limit // 2:
        return cut[: end + 1]
    space = cut.rfind(" ")
    return (cut[:space] if space >= limit // 2 else cut).rstrip(",;:- ") + "…"


def headings(text: str) -> list[str]:
    return [m.group(1).strip() for line in (text or "").splitlines() if (m := _HEADING.match(line))]


def key_points(text: str) -> list[str]:
    """Headings and bullet points, in order: the skeleton of a piece of work."""
    out: list[str] = []
    for line in (text or "").splitlines():
        if m := _HEADING.match(line):
            out.append(f"{clean(m.group(1))}:")
        elif m := _BULLET.match(line):
            out.append(f"- {summarize(m.group(1), 200)}")
    return out


def entities(text: str, limit: int = ENTITIES_PER_TEXT) -> list[str]:
    """Capitalised and technical n-grams (names, products, acronyms, identifiers), deduplicated."""
    found: dict[str, str] = {}
    flat = " ".join((text or "").split())
    # One sentence (a claim statement): its capitalised first word is usually its subject.
    one_sentence = not re.search(r"[.!?:]\s", flat.rstrip(".!? "))
    for m in _CAPS.finditer(flat):
        phrase = m.group(0).strip()
        words = phrase.split()
        start = m.start()
        sentence_start = start == 0 or flat[:start].rstrip()[-1:] in {".", "!", "?", ":", "#", "-", "*"}
        if len(words) == 1:
            low = phrase.lower()
            if low in STOPWORDS or low in _COMMON or len(phrase) < 3:
                continue
            if sentence_start and not one_sentence and not _TECH.fullmatch(phrase):
                continue  # a capitalised first word is usually just the start of a sentence
        elif words[0].lower() in STOPWORDS | _COMMON:
            phrase = " ".join(words[1:])
            if not phrase or phrase.lower() in _COMMON:
                continue
        found.setdefault(phrase.lower(), phrase)
    for m in _TECH.finditer(flat):
        token = m.group(0)
        if token.isdigit() or len(token) < 2 or token.lower() in STOPWORDS:
            continue
        if re.fullmatch(r"\d[\d.,%]*\w{0,3}", token):
            continue  # a figure ("41GW", "12%") is a value, not an entity
        if not any(token.lower() in k.split() for k in found):
            found.setdefault(token.lower(), token)
    return list(found.values())[:limit]


def _digest(*parts: str, n: int = 12) -> str:
    return hashlib.sha256("\x00".join(parts).encode()).hexdigest()[:n]


def evidence_key(url: str) -> str:
    return f"evd_{_digest(url.strip().rstrip('/').lower())}"


def entity_key(label: str) -> str:
    return f"ent_{_digest(' '.join(label.lower().split()))}"


# ---------------------------------------------------------------- the graph
@dataclass
class Node:
    key: str
    type: str
    label: str
    summary: str = ""
    body: str = ""
    author: str = ""
    status: str = ""
    confidence: float | None = None
    run_id: uuid.UUID | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    terms: dict[str, int] = field(default_factory=dict)
    length: int = 0
    prior: bool = False  # from an earlier run of the council
    row_id: uuid.UUID = field(default_factory=uuid.uuid4)

    def index(self) -> None:
        self.terms, self.length = term_counts(
            f"{self.label} {self.label} {self.summary} {self.body[:20_000]}"
        )


@dataclass(frozen=True)
class Edge:
    src: str
    dst: str
    relation: str
    author: str = ""


class CouncilGraph:
    """One run's view of its council's knowledge graph (this run's nodes plus earlier runs')."""

    def __init__(self, ctx: RunContext) -> None:
        self.ctx = ctx
        self.policy = ctx.engine.rails.graph
        self.run_id = ctx.run_id
        self.run8 = ctx.run_id.hex[:8]
        self.owner_id = ctx.owner_id
        council = getattr(ctx.sink, "council_id", None)
        self.council_id = uuid.UUID(str(council)) if council else None
        self.nodes: dict[str, Node] = {}
        self.edges: set[Edge] = set()
        self.adj: dict[str, set[Edge]] = defaultdict(set)
        self._new: dict[str, Node] = {}
        self._dirty: set[str] = set()
        self._new_edges: list[Edge] = []
        self._added = [0, 0]  # nodes, edges since the last graph.updated
        self._thoughts: OrderedDict[str, str] = OrderedDict()
        self._step_sources: dict[str, set[str]] = defaultdict(set)
        self._by_n: dict[int, str] = {}
        self._notes_seen = 0
        self._results = 0
        self._lock = asyncio.Lock()
        self._flushing: asyncio.Task[None] | None = None
        self._again = False
        self._load: asyncio.Future[None] | None = None
        self._recall: str | None = None
        self.closed = False
        self.saved = {"raw_chars": 0, "sent_chars": 0}

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        self.ctx.sink.listeners.append(self.on_event)
        closers = self.ctx.extra.get("closers")
        if isinstance(closers, list):
            closers.append(self.close)
        for src in list(self.ctx.sources.values()):
            self.add_evidence(src)
        self.sync_notes()
        with contextlib.suppress(RuntimeError):
            self._load = asyncio.get_running_loop().create_task(self._load_prior(), name="graph-load")
            # A failed load is logged by ready(); never an "exception was never retrieved" warning.
            self._load.add_done_callback(lambda t: t.cancelled() or t.exception())

    async def ready(self) -> None:
        """Wait (briefly) until the council's earlier knowledge is loaded."""
        if self._load is None:
            self._load = asyncio.ensure_future(self._load_prior())
        try:
            await asyncio.wait_for(asyncio.shield(self._load), LOAD_TIMEOUT_S)
        except Exception as exc:
            log.warning("graph.load_failed", error=f"{type(exc).__name__}: {str(exc)[:200]}")

    async def close(self) -> None:
        """Write what is still pending; called with the run's closers before its final status."""
        await self.flush()
        self.closed = True

    def _scope(self, model: Any) -> list[Any]:
        cond: list[Any] = [model.owner_id == self.owner_id]
        cond.append(model.council_id == self.council_id if self.council_id else model.council_id.is_(None))
        return cond

    async def _load_prior(self) -> None:
        prior_run = or_(GraphNode.run_id.is_(None), GraphNode.run_id != self.run_id)
        async with self.ctx.sessionmaker() as s:
            rows = (
                await s.execute(
                    select(GraphNode)
                    .where(*self._scope(GraphNode), prior_run)
                    .order_by(GraphNode.created_at.desc())
                    .limit(PRIOR_NODES)
                )
            ).scalars()
            for r in rows:
                if r.key in self.nodes:
                    continue  # this run's own node, or a newer copy of the same key
                self.nodes[r.key] = Node(
                    key=r.key,
                    type=r.type,
                    label=r.label,
                    summary=r.summary,
                    body=r.body,
                    author=r.author,
                    status=r.status,
                    confidence=r.confidence,
                    run_id=r.run_id,
                    meta=dict(r.meta or {}),
                    terms=dict(r.terms or {}),
                    length=r.length,
                    prior=True,
                    row_id=r.id,
                )
            edge_rows = (
                await s.execute(
                    select(GraphEdge.src, GraphEdge.dst, GraphEdge.relation, GraphEdge.author)
                    .where(
                        *self._scope(GraphEdge),
                        or_(GraphEdge.run_id.is_(None), GraphEdge.run_id != self.run_id),
                    )
                    .order_by(GraphEdge.created_at.desc())
                    .limit(PRIOR_EDGES)
                )
            ).all()
        for src, dst, relation, author in edge_rows:
            self._link(Edge(src, dst, relation, author), persist=False)

    # ------------------------------------------------------------ building
    def _name(self, member_id: str | None, fallback: str = "") -> str:
        bus = self.ctx.extra.get("council_bus")
        member = bus.members.get(member_id) if bus is not None and member_id else None
        return member.name if member is not None else (fallback or member_id or "")

    def _put(self, node: Node) -> Node:
        """Add a node of this run, or update this run's node with the same key."""
        node.body = node.body[:BODY_CHARS]
        node.summary = node.summary or summarize(node.body or node.label)
        node.label = node.label[:300]
        node.run_id = self.run_id
        node.index()
        old = self.nodes.get(node.key)
        if old is not None and not old.prior:
            node.row_id = old.row_id
            self.nodes[node.key] = node
            if node.key not in self._new:
                self._dirty.add(node.key)
            else:
                self._new[node.key] = node
        else:
            self.nodes[node.key] = node
            self._new[node.key] = node
            self._added[0] += 1
        self._changed()
        return node

    def _shared(self, key: str, make: Any) -> Node:
        """A node shared across runs (an entity or a source): reuse it when the council has it."""
        node = self.nodes.get(key)
        return node if node is not None else self._put(make())

    def _link(self, edge: Edge, *, persist: bool = True) -> bool:
        if edge.relation not in RELATIONS or edge.src == edge.dst or not edge.src or not edge.dst:
            return False
        if edge in self.edges or any(
            e.src == edge.src and e.dst == edge.dst and e.relation == edge.relation
            for e in self.adj[edge.src]
        ):
            return False
        self.edges.add(edge)
        self.adj[edge.src].add(edge)
        self.adj[edge.dst].add(edge)
        if persist:
            self._new_edges.append(edge)
            self._added[1] += 1
            self._changed()
        return True

    def link(self, src: str, dst: str, relation: str, author: str = "") -> bool:
        return self._link(Edge(src, dst, relation, author[:120]))

    def _mentions(self, key: str, text: str, author: str) -> None:
        for label in entities(text):
            ent = self._shared(
                entity_key(label), lambda label=label: Node(entity_key(label), "entity", label, label)
            )
            self.link(key, ent.key, "mentions", author)

    def _cite_refs(self, key: str, text: str, author: str) -> None:
        for n in {int(x) for x in _SOURCE_REF.findall(text or "")}:
            if n in self._by_n:
                self.link(key, self._by_n[n], "cites", author)

    def add_evidence(self, src: dict[str, Any], step_id: str | None = None) -> Node:
        url = str(src.get("url") or src.get("source_id") or src.get("title") or "")
        n = src.get("n")
        title = str(src.get("title") or url)
        snippet = str(src.get("snippet") or "")
        label = f"[Source {n}] {title}" if n is not None else title
        key = evidence_key(url)
        node = self.nodes.get(key)
        if node is None or (node.prior and node.meta.get("n") != n):
            node = self._put(
                Node(
                    key,
                    "evidence",
                    label,
                    summarize(snippet or title),
                    f"{title}\n{url}\n\n{snippet}".strip(),
                    meta={"url": url, "n": n, "source_id": src.get("source_id")},
                )
            )
        if isinstance(n, int):
            self._by_n[n] = key
        if step_id:
            self._step_sources[step_id].add(key)
        return node

    def _evidence_for(self, ref: str, author: str) -> str | None:
        """The node an evidence reference names: ``[Source n]``, a URL or a claim id."""
        ref = (ref or "").strip()
        m = _SOURCE_REF.search(ref)
        if m:
            return self._by_n.get(int(m.group(1)))
        if ref in self.nodes:
            return ref
        if ref.startswith(("http://", "https://")):
            return self.add_evidence({"url": ref, "title": ref}).key
        return None

    def result_key(self, step_id: str) -> str:
        return f"res_{self.run8}_{step_id}"[:64]

    def add_result(
        self,
        key: str,
        label: str,
        body: str,
        *,
        author: str = "",
        step_id: str | None = None,
        depends_on: Iterable[str] = (),
    ) -> Node:
        node = self._put(
            Node(key, "result", label or "Result", body=body, author=author, meta={"step_id": step_id})
        )
        for evd in self._step_sources.get(step_id or "", ()):
            self.link(key, evd, "cites", author)
        self._cite_refs(key, body, author)
        for dep in depends_on:
            self.link(key, self.result_key(dep), "derived_from", author)
        self._mentions(key, " . ".join([label, *headings(body)]), author)
        return node

    def ensure_result(self, step: Any) -> Node:
        """The result node of a finished plan step (made from its output if no event made it)."""
        key = self.result_key(step.id)
        node = self.nodes.get(key)
        if node is None or not node.body:
            node = self.add_result(
                key,
                step.title,
                step.output or "",
                author=step.assignee.name,
                step_id=step.id,
                depends_on=step.depends_on,
            )
        return node

    def _task_done(self, env: Envelope) -> None:
        data = env.data
        phase = str(data.get("phase") or "")
        if not data.get("ok") or not (phase in _RESULT_PHASES or phase.startswith(_RESULT_PREFIXES)):
            return
        text = data.get("text") or self._thoughts.get(str(data.get("thought_event_id") or ""), "")
        # Every member can query the graph: what a member with a secret stance wrote is concealed
        # before it lands here (its raw thought is the fallback text above).
        text = self._conceal(data.get("member_id"), str(text or ""))
        if not text:
            return
        author = self._name(data.get("member_id"), env.actor.name or "")
        if phase == "execute" and env.step_id:
            key, deps = self.result_key(env.step_id), self._depends_on(env.step_id)
        else:
            self._results += 1
            key, deps = f"res_{self.run8}_{_digest(str(data.get('key') or self._results), n=10)}", []
        self.add_result(
            key,
            str(data.get("step_title") or phase),
            text,
            author=author,
            step_id=env.step_id,
            depends_on=deps,
        )

    def _conceal(self, member_id: Any, text: str) -> str:
        from argus.runtime.council_bus import conceal

        bus = self.ctx.extra.get("council_bus")
        member = (getattr(bus, "members", None) or {}).get(str(member_id or ""))
        return conceal(member, text)

    def _depends_on(self, step_id: str) -> list[str]:
        bus = self.ctx.extra.get("council_bus")
        for st in getattr(getattr(bus, "o", None), "steps", None) or []:
            if st.id == step_id:
                return list(st.depends_on)
        return []

    def note_key(self, note_id: Any) -> str:
        return f"nte_{self.run8}_{note_id}"

    def sync_notes(self) -> None:
        """Board notes not yet in the graph (the board is the run's ``ctx.notes``)."""
        notes = self.ctx.notes
        for note in notes[self._notes_seen :]:
            if note.get("inherited_from"):
                continue  # already in the council graph, from the run that wrote it
            title, body = str(note.get("title", "")), str(note.get("body", ""))
            author = str(note.get("author", ""))
            kind = (
                "decision"
                if title.startswith("Review of")
                else "question"
                if title.endswith("?")
                else "result"
            )
            key = self.note_key(note.get("id"))
            self._put(Node(key, kind, title, body=body, author=author, meta={"note": note.get("id")}))
            if note.get("source"):
                evd = self._evidence_for(str(note["source"]), author)
                if evd:
                    self.link(key, evd, "cites", author)
            self._cite_refs(key, body, author)
            if title.startswith("Step done: "):
                done = title[len("Step done: ") :]
                for n in list(self.nodes.values()):
                    if n.type == "result" and not n.prior and n.label == done and n.key.startswith("res_"):
                        self.link(key, n.key, "derived_from", author)
                        break
            else:
                self._mentions(key, title, author)
        self._notes_seen = len(notes)

    # debate (section 2): consumed from its events, so the debate code never calls the graph
    def add_claim(self, data: dict[str, Any], *, author: str = "", step_id: str | None = None) -> Node | None:
        key = str(data.get("claim_id") or "")[:64]
        statement = str(data.get("statement") or "")
        if not key or not statement:
            return None
        evidence = [e for e in data.get("evidence") or [] if isinstance(e, dict)]
        body = statement + "".join(
            f"\n- {e.get('source', '')}" + (f': "{e["quote"]}"' if e.get("quote") else "") for e in evidence
        )
        conf = data.get("confidence")
        node = self._put(
            Node(
                key,
                "claim",
                summarize(statement, 200),
                summarize(statement),
                body,
                author=author,
                status=str(data.get("status") or "open"),
                confidence=float(conf) if isinstance(conf, int | float) else None,
                meta={"step_id": step_id, "author_id": data.get("author_id")},
            )
        )
        for e in evidence:
            ref = self._evidence_for(str(e.get("source") or ""), author)
            if ref and self.nodes.get(ref) and self.nodes[ref].type == "claim":
                self.link(ref, key, "supports", author)
            elif ref:
                self.link(key, ref, "cites", author)
        if step_id:
            self.link(key, self.result_key(step_id), "derived_from", author)
        self._mentions(key, statement, author)
        return node

    def set_status(self, key: str, status: str) -> None:
        node = self.nodes.get(key)
        if node is None or node.status == status:
            return
        node.status = status[:24]
        if node.key not in self._new:
            self._dirty.add(node.key)
        self._changed()

    def add_challenge(self, data: dict[str, Any], *, author: str = "") -> Node | None:
        key = str(data.get("challenge_id") or "")[:64]
        objection = str(data.get("objection") or "")
        if not key:
            return None
        target = str(data.get("claim_id") or data.get("target_id") or "")
        node = self._put(
            Node(
                key,
                "question",
                summarize(objection, 200) or "Challenge",
                summarize(objection),
                objection,
                author=author,
                status="open",
                meta={
                    "challenge": True,
                    "target_id": data.get("target_id"),
                    "claim_id": data.get("claim_id"),
                    "demand": data.get("demand"),
                    "round": data.get("round"),
                },
            )
        )
        if target in self.nodes:
            self.link(key, target, "about", author)
        return node

    def add_response(self, data: dict[str, Any], *, author: str = "") -> None:
        node = self.nodes.get(str(data.get("challenge_id") or ""))
        if node is None:
            return
        stance, argument = str(data.get("stance") or ""), str(data.get("argument") or "")
        node.body = f"{node.body}\n\n{author} ({stance}): {argument}"[:BODY_CHARS]
        node.index()
        claim = node.meta.get("claim_id")
        for e in data.get("evidence") or []:
            ref = self._evidence_for(str(e.get("source") if isinstance(e, dict) else e), author)
            if ref and claim:
                self.link(str(claim), ref, "cites", author)
        if node.key not in self._new:
            self._dirty.add(node.key)
        self.set_status(node.key, "open" if data.get("unsupported") else "answered")
        self._changed()

    def resolve(self, data: dict[str, Any], *, author: str = "") -> None:
        key = str(data.get("challenge_id") or "")
        node = self.nodes.get(key)
        if node is None:
            return
        outcome = str(data.get("outcome") or "")
        self.set_status(key, f"closed:{outcome}"[:24])
        claim = node.meta.get("claim_id")
        if outcome == "rejected" and claim:
            self.link(key, str(claim), "refutes", author)

    # ------------------------------------------------------------ events
    def on_event(self, env: Envelope) -> None:
        """EventSink listener (sync, inside the sink's lock): fold the run's events into the graph."""
        if self.closed:
            return
        t, data = env.type, env.data
        try:
            if t == "thought.completed":
                self._thoughts[env.id] = str(data.get("text") or "")
                while len(self._thoughts) > THOUGHTS_KEPT:
                    self._thoughts.popitem(last=False)
            elif t == "task.completed":
                self._task_done(env)
            elif t == "citation.added":
                self.add_evidence(data, env.step_id)
            elif (
                t == "a2a.message" and (data.get("message") or {}).get("metadata", {}).get("form") == "board"
            ):
                self.sync_notes()
            elif t == "debate.claim":
                self.add_claim(
                    data, author=self._name(data.get("author_id"), env.actor.name or ""), step_id=env.step_id
                )
            elif t == "debate.claim_status":
                self.set_status(str(data.get("claim_id") or ""), str(data.get("status") or ""))
            elif t == "debate.challenge":
                self.add_challenge(data, author=self._name(data.get("challenger_id"), env.actor.name or ""))
            elif t == "debate.response":
                self.add_response(data, author=self._name(data.get("responder_id"), env.actor.name or ""))
            elif t == "debate.resolution":
                self.resolve(data, author=env.actor.name or "")
            elif t == "debate.consensus":
                for c in data.get("claims") or []:
                    if isinstance(c, dict) and c.get("claim_id") and c.get("agreement"):
                        self.set_status(str(c["claim_id"]), str(c["agreement"]))
        except Exception as exc:  # the graph is an aid, never a reason for an event to fail
            log.warning("graph.ingest_failed", type=t, error=f"{type(exc).__name__}: {str(exc)[:200]}")

    # ------------------------------------------------------------ persistence
    def _changed(self) -> None:
        if self.closed:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._flushing is not None and not self._flushing.done():
            self._again = True
            return
        self._flushing = loop.create_task(self._flush_loop(), name="graph-flush")

    async def _flush_loop(self) -> None:
        while True:
            self._again = False
            await self._write()
            await self._announce()
            if not self._again:
                return

    async def flush(self) -> None:
        """Wait until everything added so far is in the database."""
        while self._flushing is not None and not self._flushing.done():
            with contextlib.suppress(Exception):
                await asyncio.shield(self._flushing)
        await self._write()
        await self._announce()

    async def _write(self) -> None:
        async with self._lock:
            new, self._new = list(self._new.values()), {}
            dirty = [self.nodes[k] for k in self._dirty if k in self.nodes]
            edges, self._new_edges = self._new_edges, []
            self._dirty = set()
            if not (new or dirty or edges):
                return
            try:
                async with self.ctx.sessionmaker() as s:
                    for n in new:
                        s.add(
                            GraphNode(
                                id=n.row_id,
                                owner_id=self.owner_id,
                                council_id=self.council_id,
                                run_id=self.run_id,
                                key=n.key,
                                type=n.type,
                                label=n.label,
                                summary=n.summary,
                                body=n.body,
                                author=n.author[:120],
                                status=n.status,
                                confidence=n.confidence,
                                terms=n.terms,
                                length=n.length,
                                meta=n.meta,
                            )
                        )
                    for e in edges:
                        s.add(
                            GraphEdge(
                                owner_id=self.owner_id,
                                council_id=self.council_id,
                                run_id=self.run_id,
                                src=e.src,
                                dst=e.dst,
                                relation=e.relation,
                                author=e.author[:120],
                            )
                        )
                    await s.flush()
                    for n in dirty:
                        await s.execute(
                            update(GraphNode)
                            .where(GraphNode.id == n.row_id)
                            .values(
                                label=n.label,
                                summary=n.summary,
                                body=n.body,
                                status=n.status,
                                confidence=n.confidence,
                                terms=n.terms,
                                length=n.length,
                                meta=n.meta,
                            )
                        )
                    await s.commit()
            except Exception as exc:
                log.warning("graph.write_failed", error=f"{type(exc).__name__}: {str(exc)[:200]}")

    async def _announce(self) -> None:
        nodes, edges = self._added
        if not (nodes or edges):
            return
        self._added = [0, 0]
        try:
            await self.ctx.sink.emit("graph.updated", {"nodes": nodes, "edges": edges})
        except Exception as exc:
            log.warning("graph.announce_failed", error=str(exc)[:200])

    # ------------------------------------------------------------ retrieval
    def _neighbours(self, key: str) -> list[tuple[Edge, str]]:
        out = []
        for e in self.adj.get(key, ()):
            other = e.dst if e.src == key else e.src
            if other in self.nodes:
                out.append((e, other))
        return sorted(out, key=lambda p: (p[0].relation, p[1]))

    def search(
        self,
        query: str,
        *,
        types: Iterable[str] = (),
        hops: int = 1,
        limit: int = 8,
        prior_only: bool = False,
    ) -> list[tuple[float, Node, int]]:
        """BM25 over node terms, then ``hops`` of expansion along edges (an entity is a hub: the
        nodes that mention it count as one hop). Returns ``(score, node, hop)``, best first."""
        wanted = set(types)
        pool = [n for n in self.nodes.values() if (n.prior or not prior_only)]
        terms = tokenize(query)
        if terms:
            hits = rank(terms, [Doc(n.key, n.terms, n.length) for n in pool], max(limit * 4, 20))
        else:  # no query: the newest nodes of this run first
            hits = [(0.0, n.key) for n in reversed(pool)][: limit * 4]
        found: dict[str, tuple[float, int]] = {}
        for score, key in hits:
            if not wanted or self.nodes[key].type in wanted:
                found.setdefault(key, (score, 0))
            if len(found) >= limit:
                break
        frontier = list(found)
        for hop in range(1, max(0, hops) + 1):
            nxt: list[str] = []
            for key in frontier:
                base = found.get(key, (0.0, 0))[0]
                for _, other in self._neighbours(key):
                    reach = [other]
                    if self.nodes[other].type == "entity":
                        reach += [o for _, o in self._neighbours(other)]
                    for o in reach:
                        node = self.nodes[o]
                        if o in found or (prior_only and not node.prior):
                            continue
                        if wanted and node.type not in wanted:
                            continue
                        found[o] = (round(base * 0.5, 4), hop)
                        nxt.append(o)
            frontier = nxt
        ranked = sorted(found.items(), key=lambda kv: (kv[1][1], -kv[1][0]))
        return [(s, self.nodes[k], h) for k, (s, h) in ranked[: limit * 2]]

    @staticmethod
    def line(node: Node, limit: int = 240) -> str:
        tags = node.type + (f", {node.status}" if node.status else "")
        if node.confidence is not None:
            tags += f", confidence {node.confidence:g}"
        who = f" — {node.author}" if node.author else ""
        when = " (earlier run)" if node.prior else ""
        text = node.summary if node.summary and node.summary != node.label else ""
        head = f"[{node.key}] ({tags}) {node.label}"
        out = f"{head}: {text}" if text else head
        out = out if len(out) <= limit else out[: limit - 1].rstrip() + "…"
        return f"{out}{who}{when}"

    def render_query(self, query: str, *, types: Iterable[str] = (), hops: int = 1, limit: int = 8) -> str:
        results = self.search(query, types=types, hops=hops, limit=limit)
        if not results:
            return "Nothing in the council's graph matches. Try other words, or graph_query with no types."
        lines = []
        for score, node, hop in results:
            prefix = "-" if hop == 0 else "  ↳"
            lines.append(
                f"{prefix} {self.line(node)}" + (f" (score {score:g})" if hop == 0 and score else "")
            )
            if hop == 0:
                near = [f"{e.relation} {o}" for e, o in self._neighbours(node.key)[:6]]
                if near:
                    lines.append(f"    edges: {', '.join(near)}")
        return "\n".join(lines) + "\n(Summaries only: graph_read gives a node's full text and edges.)"

    def read(self, key: str) -> str | None:
        node = self.nodes.get(key.strip())
        if node is None:
            return None
        head = self.line(node, limit=400)
        body = node.body or node.summary or node.label
        if len(body) > READ_CHARS:
            body = body[:READ_CHARS] + f"\n[… {len(node.body) - READ_CHARS} more chars]"
        out = [head, "", body]
        near = self._neighbours(node.key)
        if near:
            out += ["", "Edges:"]
            for e, other in near[:40]:
                arrow = f"{e.relation} →" if e.src == node.key else f"← {e.relation}"
                out.append(f"- {arrow} {self.line(self.nodes[other], 160)}")
        return "\n".join(out)

    # ------------------------------------------------------------ context assembly
    def claims_of(self, result_key: str) -> list[Node]:
        return [
            self.nodes[e.src]
            for e in sorted(self.adj.get(result_key, ()), key=lambda e: e.src)
            if e.dst == result_key
            and e.relation == "derived_from"
            and e.src in self.nodes
            and self.nodes[e.src].type == "claim"
        ]

    def dependency_digest(self, step: Any) -> str:
        """One dependency in at most ``digest_chars``: summary, claims, key points, a pointer."""
        cap = self.policy.digest_chars
        node = self.ensure_result(step)
        output = step.output or ""
        head = f"## {step.title} ({step.assignee.name}) [{node.key}]"
        claims = self.claims_of(node.key)
        if len(head) + 1 + len(output) <= cap and not claims:
            return f"{head}\n{output}"
        tail = f'(Full text: {len(output):,} chars, graph_read("{node.key}"))'
        lines = [head, f"Summary: {node.summary}"]
        used = sum(len(x) + 1 for x in lines) + len(tail) + 1
        extra: list[str] = []
        if claims:
            extra.append("Claims:")
            extra += [f"- [{c.key}] ({c.status or 'open'}) {summarize(c.label, 160)}" for c in claims]
        points = key_points(output)
        if points:
            extra.append("Key points:")
            extra += points
        for x in extra:
            if used + len(x) + 1 > cap:
                break
            lines.append(x)
            used += len(x) + 1
        if lines[-1].endswith(":"):
            lines.pop()  # a section title with nothing under it
        return "\n".join([*lines, tail])

    def upstream_digest(self, deps: list[Any]) -> str:
        """The dependencies' digests; the pointer to graph_read only when one of them is cut."""
        if not deps:
            return ""
        blocks = [self.dependency_digest(d) for d in deps]
        cut = any(
            b.endswith(f'graph_read("{self.result_key(d.id)}"))') for b, d in zip(blocks, deps, strict=True)
        )
        return "\n\n".join(blocks) + (f"\n\n{POINTER}" if cut else "")

    def board_text(self, notes: list[dict[str, Any]], cap: int) -> str:
        """The board as compact summaries (newest first within ``cap``) plus the open challenges."""
        self.sync_notes()
        lines: list[str] = []
        used = 0
        for n in reversed(notes):
            body = str(n.get("body", ""))
            short = summarize(body)
            earlier = " (earlier run)" if n.get("inherited_from") else ""
            line = f"#{n['id']} [{n.get('author', '?')}{earlier}] {n.get('title', '')}: {short}"
            if n.get("source"):
                line += f" (source: {n['source']})"
            if len(short) < len(clean(body)):
                # An inherited note's node belongs to the run that wrote it: notes_read shows it whole.
                line += " [notes_read]" if earlier else f" [{self.note_key(n['id'])}]"
            if used + len(line) > cap:
                lines.append(
                    f"(+{len(notes) - len(lines)} older notes: notes_read or graph_query shows them)"
                )
                break
            lines.append(line)
            used += len(line)
        text = "\n".join(reversed(lines))
        open_ = [
            n
            for n in self.nodes.values()
            if n.type == "question" and n.meta.get("challenge") and n.status == "open" and not n.prior
        ]
        if open_:
            rows = []
            for c in open_[-10:]:
                target = str(c.meta.get("target_id") or "")
                on = f" on {c.meta['claim_id']}" if c.meta.get("claim_id") else ""
                demand = c.meta.get("demand") or "challenge"
                rows.append(
                    f"- [{c.key}] {c.author} → {self._name(target, target)}{on} ({demand}): "
                    + summarize(c.label, 180)
                )
            text = (text + "\n" if text else "") + "Open challenges:\n" + "\n".join(rows)
        return text

    def recall_text(self) -> str:
        """The "What this council already knows" section: earlier runs' nodes that match the
        topic (cached once loaded, so the stable part of the system prompt stays stable)."""
        if self._recall is not None:
            return self._recall
        if self._load is None or not self._load.done():
            return ""
        k = self.policy.council_recall
        if k <= 0:
            self._recall = ""
            return ""
        query = f"{self.ctx.topic} {self.ctx.description}"
        primary = max(1, (2 * k + 2) // 3)
        hits = [
            (s, n, h)
            for s, n, h in self.search(query, hops=1, limit=primary, prior_only=True)
            if n.type != "entity"
        ][:k]
        if not hits:
            self._recall = ""
            return ""
        lines = [f"- {self.line(n, 220)}" for _, n, _ in hits]
        self._recall = (
            "\n## What this council already knows (from its earlier runs; graph_read gives the full "
            "text, graph_query searches more)\n" + "\n".join(lines)
        )
        return self._recall

    async def context_saved(self, member: Any, task: str, raw_chars: int, sent_chars: int) -> None:
        self.saved["raw_chars"] += raw_chars
        self.saved["sent_chars"] += sent_chars
        await self.ctx.sink.emit(
            "context.saved",
            {"member_id": member.id, "task": task[:200], "raw_chars": raw_chars, "sent_chars": sent_chars},
            actor=member.actor,
        )

    def register_tools(self, tools: ToolRegistry) -> None:
        from argus.tools.graph import graph_tools

        for tool in graph_tools():
            if tool.name not in tools:
                tools.register(tool)


def graph_for(ctx: RunContext, tools: ToolRegistry | None = None) -> CouncilGraph | None:
    """The run's graph (created on first use), or None when ``guardrails.graph.enabled`` is off.

    With ``tools``, the graph tools are added to that registry, so they exist only when the graph
    does.
    """
    if not ctx.engine.rails.graph.enabled:
        return None
    graph = ctx.extra.get("graph")
    if graph is None:
        graph = CouncilGraph(ctx)
        ctx.extra["graph"] = graph
        graph.start()
    if tools is not None:
        graph.register_tools(tools)
    return graph
