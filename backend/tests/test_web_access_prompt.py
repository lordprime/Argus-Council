"""Agents are told exactly what web access they have and how to search with it, so a member with
web_fetch or the browser never tells the user it cannot reach the web."""

from __future__ import annotations

from argus.browser.client import RemoteBrowser
from argus.runtime.agent import AgentRunner, MemberSpec
from argus.runtime.oversight import ThoughtBus
from argus.tools.base import ToolRegistry
from argus.tools.browser import Browse
from argus.tools.skills import skill_tools
from argus.tools.web import WebFetch
from tests import test_limit_extension as limits
from tests.test_graph_context import FakeLLM, _council, _ctx, _owner, reply

db = limits.db  # the same fixture


def _member(tools: list[str]) -> MemberSpec:
    return MemberSpec(
        id="m1", name="Ada", role="researcher", provider="fake", model="m", system_prompt="", tools=tools
    )


async def _prompt(db, tools: list[str], registry: list) -> str:
    owner = await _owner(db)
    council = await _council(db, owner)
    ctx, _ = await _ctx(db, owner, council, FakeLLM(lambda r: reply("done")))
    runner = AgentRunner(_member(tools), ctx, ToolRegistry(registry), ThoughtBus(ctx, []), "brief")
    return runner._system_prompt()


def _web():
    return [WebFetch(), Browse(RemoteBrowser("unix:/nonexistent.sock", "t")), *skill_tools()]


async def test_a_member_with_the_browser_is_told_it_has_it_and_how_to_search(db):
    prompt = await _prompt(db, ["web_fetch", "browse"], _web())
    assert "Your web access" in prompt and "browse" in prompt and "web_fetch" in prompt
    assert "html.duckduckgo.com/html/?q=" in prompt  # no web_search here: search through a results page
    assert "never tell the user you cannot reach the web" in prompt.lower()


async def test_a_member_without_web_tools_is_told_to_ask_one_who_has_them(db):
    prompt = await _prompt(db, ["notes_read"], _web())
    assert "Your web access" not in prompt
    assert "no web tools" in prompt.lower() and "ask_agent" in prompt


def test_tool_descriptions_allow_a_search_results_page():
    assert "html.duckduckgo.com/html/?q=" in Browse.description
    assert "html.duckduckgo.com/html/?q=" in WebFetch.description
