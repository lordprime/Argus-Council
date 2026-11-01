"""Live data tools (market_data, news_search), the live-information roles and their council picks."""

import json

import httpx

from argus.agents.roles import DEFAULT_ROSTERS, ROLES
from argus.runtime.guardrails import Guardrails
from argus.services.autocouncil import heuristic_plan
from argus.tools.market import MarketData, NewsSearch, parse_feed, stooq_symbol
from argus.tools.web import web_tools
from tests.test_exports_and_tools import _Ctx, private, public

DAY = 86_400
T0 = 1_761_800_000  # 2025-10-30T04:53:20Z


def chart(symbol="AAPL", closes=(100.0, 102.0, None, 99.0), price=101.5, market_time=None, prev=None):
    meta = {
        "symbol": symbol,
        "longName": "Apple Inc.",
        "currency": "USD",
        "fullExchangeName": "NasdaqGS",
        "instrumentType": "EQUITY",
        "regularMarketPrice": price,
        "regularMarketTime": market_time or T0 + (len(closes) - 1) * DAY + 3600,
    }
    if prev is not None:
        meta["previousClose"] = prev
    return {
        "chart": {
            "result": [
                {
                    "meta": meta,
                    "timestamp": [T0 + i * DAY for i in range(len(closes))],
                    "indicators": {"quote": [{"close": list(closes)}]},
                }
            ],
            "error": None,
        }
    }


def tool(cls, handler, resolver=public):
    return cls(
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)), resolver=resolver
    )


async def test_market_quote_reports_price_change_currency_and_market_time():
    seen = []

    def handler(req):
        seen.append(req.url)
        return httpx.Response(200, json=chart())

    ctx = _Ctx()
    res = await tool(MarketData, handler).invoke({"symbols": "AAPL"}, ctx)
    assert not res.is_error, res.content
    assert seen[0].host == "query1.finance.yahoo.com" and seen[0].params["range"] == "5d"
    # The last bar is the market day, so the previous close is the bar before it (102 -> 101.5).
    assert "Price 101.50 USD" in res.content and "previous close 102.00" in res.content
    assert "change -0.49%" in res.content and "Market time 2025-11-02T05:53:20Z" in res.content
    assert ctx.events[0][0] == "citation.added" and "query1.finance.yahoo.com" in ctx.events[0][1]["url"]
    assert "retrieved_at" in ctx.run.sources[str(seen[0])]


async def test_market_history_has_stats_and_a_compact_table():
    closes = [100.0 + i for i in range(60)]
    closes[10] = 50.0
    res = await tool(MarketData, lambda r: httpx.Response(200, json=chart(closes=closes, prev=158.0))).invoke(
        {"symbols": ["AAPL"], "period": "3mo"}, _Ctx()
    )
    assert not res.is_error, res.content
    assert "previous close 158.00" in res.content
    assert "Low 50.00 (2025-11-09)" in res.content and "High 159.00" in res.content
    assert "Change 59.0%" in res.content and "60 closes" in res.content
    rows = [ln for ln in res.content.splitlines() if ln.strip()[:4].isdigit()]
    assert len(rows) == 24 and rows[0].strip().startswith("2025-10-30") and "159.00" in rows[-1]


async def test_market_falls_back_to_stooq_and_reports_failures_per_symbol():
    def handler(req):
        if req.url.host == "query1.finance.yahoo.com":
            return httpx.Response(404, json={"chart": {"result": None, "error": {"code": "Not Found"}}})
        assert req.url.params["s"] == "msft.us"
        body = "Symbol,Date,Time,Open,High,Low,Close,Volume,Name\nMSFT.US,2025-10-31,22:00:00,1,2,0.5,512.3,10,MICROSOFT\n"
        return httpx.Response(200, text=body, headers={"content-type": "application/octet-stream"})

    res = await tool(MarketData, handler).invoke({"symbols": ["MSFT", "BTC-USD", "bad sym!"]}, _Ctx())
    assert not res.is_error
    assert "Price 512.30" in res.content and "via Stooq" in res.content
    assert "BTC-USD: no data (Yahoo Finance: HTTP 404" in res.content
    assert "bad sym!: not a valid symbol" in res.content
    assert res.data["failed"] == 2
    assert stooq_symbol("EURUSD=X") == "eurusd" and stooq_symbol("^GSPC") is None


async def test_market_respects_allowed_domains_and_private_addresses():
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(200, json=chart())

    rails = Guardrails.model_validate({"network": {"allow_domains": ["*.wikipedia.org"]}})
    res = await tool(MarketData, handler).invoke({"symbols": ["AAPL"]}, _Ctx(rails))
    assert res.is_error and "not in the allowed domains" in res.content and not calls
    res = await tool(MarketData, handler, private).invoke({"symbols": ["AAPL"]}, _Ctx())
    assert res.is_error and "private" in res.content and not calls


async def test_market_never_invents_a_price():
    empty = {"chart": {"result": [{"meta": {"symbol": "X"}, "timestamp": [], "indicators": {"quote": [{}]}}]}}
    res = await tool(MarketData, lambda r: httpx.Response(200, json=empty)).invoke(
        {"symbols": ["XYZQ"], "period": "1mo"}, _Ctx()
    )
    assert res.is_error and "no price" in res.content
    res = await tool(
        MarketData,
        lambda r: httpx.Response(200, text="not json", headers={"content-type": "application/json"}),
    ).invoke({"symbols": ["XYZQ"], "period": "1mo"}, _Ctx())
    assert res.is_error and "unreadable" in res.content


FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>News</title>
<item><title>DRAM contract prices jump 20% - TrendForce</title>
<link>https://news.example.com/a</link><pubDate>Fri, 31 Oct 2025 09:30:00 GMT</pubDate>
<source url="https://www.trendforce.com">TrendForce</source></item>
<item><title>No link</title></item>
<item><title>GPU street prices ease</title><link>https://news.example.com/b</link></item>
</channel></rss>"""


async def test_news_search_parses_the_feed_and_cites_each_article():
    seen = []

    def handler(req):
        seen.append(req.url)
        return httpx.Response(200, text=FEED, headers={"content-type": "application/rss+xml"})

    ctx = _Ctx()
    res = await tool(NewsSearch, handler).invoke(
        {"query": "DRAM prices", "when": "30d", "region": "in", "max_results": 5}, ctx
    )
    assert not res.is_error, res.content
    assert seen[0].host == "news.google.com" and seen[0].params["q"] == "DRAM prices when:30d"
    assert seen[0].params["gl"] == "IN" and seen[0].params["ceid"] == "IN:en"
    assert "[Source 1] DRAM contract prices jump 20%\n" in res.content
    assert "TrendForce · published 2025-10-31T09:30:00Z" in res.content
    assert "date not given" in res.content and "No link" not in res.content
    assert [e[1]["url"] for e in ctx.events] == ["https://news.example.com/a", "https://news.example.com/b"]


def test_news_feed_with_entities_never_expands_them():
    bomb = (
        '<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;">]>'
        "<rss><channel><item><title>&b;</title><link>https://x.example/1</link></item></channel></rss>"
    )
    try:
        items = parse_feed(bomb, 5)
    except ValueError:
        return  # refused outright
    assert "aaaa" not in json.dumps(items)


async def test_news_search_reports_blocked_and_failed_fetches():
    rails = Guardrails.model_validate({"network": {"allow_domains": ["example.com"]}})
    res = await tool(NewsSearch, lambda r: httpx.Response(200, text=FEED)).invoke({"query": "x"}, _Ctx(rails))
    assert res.is_error and "blocked" in res.content
    res = await tool(
        NewsSearch,
        lambda r: httpx.Response(200, text="<rss><channel>", headers={"content-type": "application/xml"}),
    ).invoke({"query": "x"}, _Ctx())
    assert res.is_error and "unreadable feed" in res.content


def test_live_tools_are_always_registered():
    class S:
        search_provider = None

    names = [t.name for t in web_tools(S())]
    assert names == ["web_fetch", "market_data", "news_search"]
    assert all(t.risk == frozenset({"network"}) for t in web_tools(S()))


def test_live_information_roles():
    for key in ("scout", "market_analyst", "fact_checker", "data_analyst"):
        assert key in ROLES and not ROLES[key].overseer
    assert {"news_search", "market_data", "web_fetch"} <= set(ROLES["scout"].default_tools)
    assert "market_data" in ROLES["market_analyst"].default_tools
    assert "knowledge_read" in ROLES["data_analyst"].default_tools
    for role in ROLES.values():
        if "knowledge_search" in role.default_tools:
            assert "knowledge_read" in role.default_tools, role.key
    assert "scout" in DEFAULT_ROSTERS["research"]


def test_heuristic_council_picks_the_live_roles():
    def roles(topic):
        return [s.role for s in heuristic_plan(topic, "", "research", 10)]

    assert "market_analyst" in roles("How will Nvidia stock and the USD/INR exchange rate move?")
    assert "scout" in roles("What is the latest news on DRAM supply this week?")
    assert "data_analyst" in roles("Compare the vendor quotes in my uploaded xlsx sheet")
    assert "fact_checker" in roles("Verify the figures in this press release")
    assert not {"market_analyst", "scout", "data_analyst", "fact_checker"} & set(roles("Write a poem"))
