"""Live data tools: market quotes and price history, and recent news.

Both use keyless public sources, so they work without a search provider: the Yahoo Finance chart
API for quotes and history (stocks, indices, FX such as ``USDINR=X``, crypto such as ``BTC-USD``,
futures such as ``GC=F``), with Stooq as a fallback for plain quotes, and the Google News RSS feed
for news. Every request goes through ``guarded_fetch``: the run's allowed domains, private-address
blocking, per-hop redirect checks, browser request headers and the retries of transient failures
(timeouts, 429, 502-504) apply exactly as for ``web_fetch``. Every data URL is recorded
as a citable source with its retrieval time; nothing is ever filled in when a source has no data.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Literal
from urllib.parse import quote, urlencode
from xml.etree import ElementTree

import httpx
from pydantic import BaseModel, Field

from argus.tools.base import StrList, Tool, ToolCallContext, ToolResult
from argus.tools.web import FetchBlocked, Resolver, cite, default_client, default_resolver, guarded_fetch

YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/"
STOOQ_QUOTE = "https://stooq.com/q/l/"
GOOGLE_NEWS = "https://news.google.com/rss/search"

MAX_SYMBOLS = 10
HISTORY_ROWS = 24
_SYMBOL = re.compile(r"^[A-Za-z0-9.^=\-_]{1,24}$")
Period = Literal["quote", "5d", "1mo", "3mo", "6mo", "1y", "2y", "5y"]
_JSON = ("application/json", "text/json", "text/javascript")
# Stooq serves its CSV under several content types.
_CSV = ("text/csv", "text/plain", "application/csv", "application/octet-stream")
_XML = ("application/rss+xml", "application/atom+xml", "application/xml", "text/xml")


class _LiveTool(Tool):
    risk = frozenset({"network"})
    parallel_safe = True  # read-only lookups

    def __init__(
        self,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
        resolver: Resolver | None = None,
        *,
        allow_private: bool = False,
    ) -> None:
        self.allow_private = allow_private
        self.client_factory = client_factory or (lambda: default_client(allow_private))
        self.resolver = resolver or default_resolver

    async def get(self, url: str, ctx: ToolCallContext, types: tuple[str, ...]) -> tuple[str, str]:
        """(final url, body). Raises ``FetchBlocked`` or ``httpx.HTTPError``."""
        final, _ctype, body = await guarded_fetch(
            url,
            ctx,
            client_factory=self.client_factory,
            resolver=self.resolver,
            allow_private=self.allow_private,
            types=types,
        )
        return final, body


# ------------------------------------------------------------------ market data
def _iso(ts: Any) -> str | None:
    try:
        return datetime.fromtimestamp(int(ts), UTC).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _num(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:,.2f}" if abs(value) >= 1 else f"{value:.6g}"


def _pct(new: float | None, old: float | None) -> float | None:
    if new is None or not old:
        return None
    return round((new - old) / old * 100, 2)


def parse_chart(payload: str) -> dict[str, Any]:
    """The Yahoo chart API's answer as a quote plus a daily series. Raises ``ValueError``."""
    data = json.loads(payload).get("chart") or {}
    if data.get("error"):
        err = data["error"]
        raise ValueError(str(err.get("description") or err.get("code") or "no data"))
    result = (data.get("result") or [None])[0]
    if not result:
        raise ValueError("no data returned for this symbol")
    meta = result.get("meta") or {}
    quote_ = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    closes = quote_.get("close") or []
    series = [
        (day, float(c))
        for t, c in zip(result.get("timestamp") or [], closes, strict=False)
        if c is not None and (day := _iso(t))
    ]
    price = meta.get("regularMarketPrice")
    if price is None and series:
        price = series[-1][1]
    if price is None:
        raise ValueError("no price in the response")
    market_time = _iso(meta.get("regularMarketTime"))
    prev = meta.get("previousClose")
    if prev is None and series:
        # The last bar is today's while the market is open; the previous close is the bar before.
        same_day = market_time and series[-1][0][:10] == market_time[:10]
        older = series[:-1] if same_day else series
        prev = older[-1][1] if older else None
    return {
        "symbol": meta.get("symbol"),
        "name": meta.get("longName") or meta.get("shortName") or meta.get("symbol"),
        "currency": meta.get("currency"),
        "exchange": meta.get("fullExchangeName") or meta.get("exchangeName"),
        "type": meta.get("instrumentType"),
        "price": float(price),
        "previous_close": float(prev) if prev is not None else None,
        "change_pct": _pct(float(price), float(prev) if prev is not None else None),
        "market_time": market_time,
        "series": series,
    }


def stooq_symbol(symbol: str) -> str | None:
    """Stooq's name for a plain US ticker (``AAPL`` -> ``aapl.us``) or FX pair (``EURUSD=X``)."""
    s = symbol.upper()
    if re.fullmatch(r"[A-Z]{1,5}", s):
        return f"{s.lower()}.us"
    if m := re.fullmatch(r"([A-Z]{6})=X", s):
        return m.group(1).lower()
    return None


def parse_stooq(payload: str) -> dict[str, Any]:
    rows = list(csv.DictReader(io.StringIO(payload)))
    if not rows:
        raise ValueError("no data returned")
    row = {k.strip().lower(): (v or "").strip() for k, v in rows[0].items() if k}
    close = row.get("close", "")
    if not close or close.upper() == "N/D":
        raise ValueError("no data for this symbol")
    when = " ".join(x for x in (row.get("date"), row.get("time")) if x and x.upper() != "N/D")
    return {
        "symbol": row.get("symbol", "").upper(),
        "name": row.get("name") or row.get("symbol", "").upper(),
        "currency": None,
        "exchange": None,
        "type": None,
        "price": float(close),
        "previous_close": None,
        "change_pct": None,
        "market_time": when or None,
        "series": [],
    }


def _history(series: list[tuple[str, float]]) -> list[str]:
    if not series:
        return ["  (no history returned)"]
    first, last = series[0], series[-1]
    lo = min(series, key=lambda p: p[1])
    hi = max(series, key=lambda p: p[1])
    lines = [
        f"  First {first[0][:10]} {_num(first[1])} · Last {last[0][:10]} {_num(last[1])} · "
        f"Change {_pct(last[1], first[1])}% · Low {_num(lo[1])} ({lo[0][:10]}) · "
        f"High {_num(hi[1])} ({hi[0][:10]}) · {len(series)} closes"
    ]
    if len(series) <= HISTORY_ROWS:
        picked = series
    else:  # evenly spaced closes, always keeping the first and the last
        step = (len(series) - 1) / (HISTORY_ROWS - 1)
        picked = [series[round(i * step)] for i in range(HISTORY_ROWS)]
    lines.append("  Date       | Close")
    lines += [f"  {d[:10]} | {_num(c)}" for d, c in picked]
    return lines


class MarketData(_LiveTool):
    name = "market_data"
    ledger_peer = "query1.finance.yahoo.com"  # the protocol ledger's peer for these requests
    description = (
        "Live market data (no API key): the latest price, previous close and change for stocks "
        "(AAPL, RELIANCE.NS, 005930.KS), indices (^GSPC, ^NSEI), FX pairs (USDINR=X), crypto (BTC-USD) "
        "and futures (GC=F, CL=F), and with `period` a daily price history with its first, last, low "
        "and high. Use Yahoo Finance symbols. Quotes may be delayed: always report the market time "
        "and currency returned, and never estimate a price that was not returned."
    )

    class Args(BaseModel):
        symbols: StrList = Field(min_length=1, max_length=MAX_SYMBOLS)
        period: Period = Field(default="quote", description="'quote' for the latest price only")

    async def _yahoo(self, symbol: str, period: Period, ctx: ToolCallContext) -> tuple[str, dict[str, Any]]:
        span = "5d" if period == "quote" else period
        interval = "1wk" if period in ("2y", "5y") else "1d"
        url = f"{YAHOO_CHART}{quote(symbol, safe='')}?" + urlencode({"range": span, "interval": interval})
        final, body = await self.get(url, ctx, _JSON)
        try:
            return final, parse_chart(body)
        except (json.JSONDecodeError, AttributeError, TypeError) as exc:
            raise ValueError("unreadable response") from exc

    async def _stooq(self, symbol: str, ctx: ToolCallContext) -> tuple[str, dict[str, Any]]:
        code = stooq_symbol(symbol)
        if code is None:
            raise ValueError("no fallback source for this symbol")
        url = f"{STOOQ_QUOTE}?" + urlencode({"s": code, "f": "sd2t2ohlcvn", "h": "", "e": "csv"})
        final, body = await self.get(url, ctx, _CSV)
        return final, parse_stooq(body)

    async def one(self, symbol: str, period: Period, ctx: ToolCallContext) -> str:
        if not _SYMBOL.fullmatch(symbol):
            return f"{symbol[:30]}: not a valid symbol"
        provider = "Yahoo Finance"
        try:
            url, q = await self._yahoo(symbol, period, ctx)
        except (FetchBlocked, httpx.HTTPError, ValueError) as first:
            problem = str(first) if not isinstance(first, httpx.HTTPError) else type(first).__name__
            if period != "quote":
                return f"{symbol}: no data ({problem})"
            try:
                url, q = await self._stooq(symbol, ctx)
                provider = "Stooq"
            except (FetchBlocked, httpx.HTTPError, ValueError) as second:
                again = str(second) if not isinstance(second, httpx.HTTPError) else type(second).__name__
                return f"{symbol}: no data (Yahoo Finance: {problem}; fallback: {again})"
        source = await cite(
            ctx, url, f"{q['name']} ({symbol}) market data, {provider}", f"{q['name']} {q['price']}"
        )
        change = f"{q['change_pct']:+.2f}%" if q["change_pct"] is not None else "n/a"
        lines = [
            f"[Source {source['n']}] {q['name']} ({q['symbol'] or symbol})"
            + (f" · {q['exchange']}" if q["exchange"] else "")
            + (f" · {q['type']}" if q["type"] else ""),
            f"  Price {_num(q['price'])} {q['currency'] or '(currency not stated)'} · "
            f"previous close {_num(q['previous_close'])} · change {change}",
            f"  Market time {q['market_time'] or 'not stated'} · retrieved {source['retrieved_at']} "
            f"via {provider}",
        ]
        if period != "quote":
            lines += _history(q["series"])
        return "\n".join(lines)

    async def run(self, args: Args, ctx: ToolCallContext) -> ToolResult:
        symbols = list(dict.fromkeys(s.strip() for s in args.symbols if s.strip()))[:MAX_SYMBOLS]
        if not symbols:
            return ToolResult.error("Give at least one symbol")
        sem = asyncio.Semaphore(4)

        async def bounded(sym: str) -> str:
            async with sem:
                return await self.one(sym, args.period, ctx)

        blocks = await asyncio.gather(*(bounded(s) for s in symbols))
        failed = sum(": no data" in b or ": not a valid" in b for b in blocks)
        text = "\n\n".join(blocks)
        if failed == len(blocks):
            return ToolResult.error(text)
        return ToolResult(text, data={"symbols": symbols, "failed": failed})


# ------------------------------------------------------------------ news
_DOCTYPE = re.compile(r"<!DOCTYPE[^>\[]*(\[.*?\])?\s*>", re.IGNORECASE | re.DOTALL)
_WHEN = {"1d": " when:1d", "7d": " when:7d", "30d": " when:30d", "any": ""}


def parse_feed(payload: str, limit: int) -> list[dict[str, str]]:
    """RSS items as dicts. The DOCTYPE is removed first, so no entity is ever declared or expanded."""
    cleaned = _DOCTYPE.sub("", payload)
    if "<!ENTITY" in cleaned.upper():
        raise ValueError("feed declares entities")
    try:
        root = ElementTree.fromstring(cleaned)  # noqa: S314 - no DOCTYPE, so no entities to expand
    except ElementTree.ParseError as exc:
        raise ValueError("unreadable feed") from exc
    items = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if not title or not link.startswith(("http://", "https://")):
            continue
        src = item.find("source")
        source = (src.text or "").strip() if src is not None else ""
        if source and title.endswith(f" - {source}"):
            title = title[: -len(source) - 3]
        published = ""
        if raw := (item.findtext("pubDate") or "").strip():
            try:
                published = parsedate_to_datetime(raw).astimezone(UTC).isoformat().replace("+00:00", "Z")
            except (TypeError, ValueError):
                published = raw
        items.append({"title": title, "link": link, "source": source, "published": published})
        if len(items) >= limit:
            break
    return items


class NewsSearch(_LiveTool):
    name = "news_search"
    ledger_peer = "news.google.com"
    description = (
        "Search recent news (Google News, no API key): headlines with their outlet and publication "
        "time, newest coverage first. Use `when` to stay current (1d, 7d, 30d) and `region` (a "
        "two-letter country code such as US, IN, GB) for local coverage; read articles with web_fetch "
        "before relying on more than the headline."
    )

    class Args(BaseModel):
        query: str = Field(min_length=1, max_length=300)
        max_results: int = Field(default=8, ge=1, le=25)
        when: Literal["1d", "7d", "30d", "any"] = "7d"
        region: str = Field(default="US", pattern=r"^[A-Za-z]{2}$")

    async def run(self, args: Args, ctx: ToolCallContext) -> ToolResult:
        region = args.region.upper()
        params = {
            "q": args.query + _WHEN[args.when],
            "hl": f"en-{region}",
            "gl": region,
            "ceid": f"{region}:en",
        }
        try:
            _, body = await self.get(f"{GOOGLE_NEWS}?{urlencode(params)}", ctx, _XML)
            items = parse_feed(body, args.max_results)
        except FetchBlocked as exc:
            return ToolResult.error(f"News search blocked: {exc}")
        except httpx.HTTPError as exc:
            return ToolResult.error(f"News search failed: {type(exc).__name__}")
        except ValueError as exc:
            return ToolResult.error(f"News search failed: {exc}")
        if not items:
            return ToolResult(f"No news for: {args.query} ({args.when})", data={"results": []})
        lines = []
        for i, it in enumerate(items, 1):
            source = await cite(ctx, it["link"], f"{it['title']} ({it['source'] or 'news'})", it["title"])
            lines.append(
                f"{i}. [Source {source['n']}] {it['title']}\n"
                f"   {it['source'] or 'unknown outlet'} · published {it['published'] or 'date not given'}\n"
                f"   {it['link']}"
            )
        return ToolResult("\n".join(lines), data={"results": items})
