"""Runner mechanics for plugin CLIs (argus/runner/clis): per-turn files, result files, session ids the
CLI assigns, and the kill switch for a CLI that starts with tools of its own. Uses a stand-in plugin.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

import argus.runner.clis as clis
import argus.runner.process as process
from argus.runner.clis import CliPlugin
from argus.runner.process import Runner, RunnerConfig
from argus.runner.specs import Binaries, Command, InvokeSpec

USER = "11111111-1111-4111-8111-111111111111"

FAKE = """
import json, os, sys
prompt = sys.stdin.read()
cfg = open(os.environ["TURN_FILE"]).read()
if os.environ.get("TOOLS"):
    print(json.dumps({"type": "init", "tools": ["bash", "edit"]}), flush=True)
print(json.dumps({"type": "init", "tools": [], "sessionID": "ses_AbCdEf123456"}))
print(json.dumps({"type": "text", "text": prompt + "|" + cfg}))
open(os.environ["RESULT"], "w").write(json.dumps({"usage": {"input": 3}}))
"""


def _plugin(binary: str) -> CliPlugin:
    def build(spec, binaries, turn):
        turn_file = os.path.join(turn.turn_dir, "agent.json")
        result = os.path.join(turn.turn_dir, "out.json")
        env = {"TURN_FILE": turn_file, "RESULT": result}
        if spec.prompt.startswith("tools"):
            env["TOOLS"] = "1"
        return Command([binary], env, spec.prompt, files={turn_file: '{"tools": []}'}, result_file=result)

    def session(event):
        return event.get("sessionID")

    def tools(event):
        return event.get("tools") if event.get("type") == "init" else None

    return CliPlugin(
        name="opencode", label="x", build=build, session_from_event=session, tools_reported=tools
    )


@pytest.fixture
def runner(tmp_path: Path, monkeypatch) -> Runner:
    exe = tmp_path / "fake"
    exe.write_text(f"#!{sys.executable}\n{FAKE}")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    fake = _plugin(str(exe))
    monkeypatch.setattr(clis, "plugin", lambda cli: fake)
    monkeypatch.setattr(process, "plugin", lambda cli: fake)
    return Runner(RunnerConfig(home_root=tmp_path / "users", binaries=Binaries()))


async def test_turn_files_result_file_and_assigned_session(runner, tmp_path):
    events = [
        e async for e in runner.invoke(InvokeSpec(user_id=USER, cli="opencode", prompt="hi", run_key="r1"))
    ]
    text = next(e for e in events if e.get("type") == "text")
    assert text["text"] == 'hi|{"tools": []}'
    result = next(e for e in events if e.get("type") == "argus.result_file")
    assert result["data"] == {"usage": {"input": 3}}
    exit_ = events[-1]
    assert exit_["ok"] and exit_["session_id"] == "ses_AbCdEf123456"  # case kept: not a UUID
    turns = tmp_path / "users" / USER / ".argus" / "turns"
    assert list(turns.iterdir()) == []  # per-turn files are removed
    assert runner.has_session(USER, "opencode", "ses_AbCdEf123456")
    assert not runner.has_session(USER, "opencode", "ses_abcdef123456")
    resumed = [
        e
        async for e in runner.invoke(
            InvokeSpec(
                user_id=USER, cli="opencode", prompt="again", session_id="ses_AbCdEf123456", resume=True
            )
        )
    ]
    assert resumed[-1]["ok"]


async def test_a_cli_that_reports_tools_never_takes_the_turn(runner):
    events = [e async for e in runner.invoke(InvokeSpec(user_id=USER, cli="opencode", prompt="tools please"))]
    assert not any(e.get("type") == "text" for e in events)
    exit_ = events[-1]
    assert exit_["ok"] is False and exit_["error"] == "tools_not_disabled"


def test_session_ids_are_checked_per_cli():
    with pytest.raises(ValueError):
        InvokeSpec(user_id=USER, cli="claude", prompt="x", session_id="ses_AbCdEf123456", resume=True)
    with pytest.raises(ValueError):
        InvokeSpec(
            user_id=USER, cli="opencode", prompt="x", session_id="ses_AbCdEf123456"
        )  # assigned by the CLI
    with pytest.raises(ValueError):
        InvokeSpec(user_id=USER, cli="opencode", prompt="x", session_id="ses_../../etc", resume=True)
    with pytest.raises(ValueError):
        InvokeSpec(user_id=USER, cli="qwen", prompt="x", key_provider="Bad Provider")
    spec = InvokeSpec(user_id=USER, cli="opencode", prompt="x", session_id="ses_AbCdEf123456", resume=True)
    assert spec.session_id == "ses_AbCdEf123456"


async def test_turn_files_must_stay_in_the_users_home(runner, monkeypatch, tmp_path):
    def build(spec, binaries, turn):
        return Command(["true"], {}, "", files={str(tmp_path / "outside.txt"): "x"})

    bad = CliPlugin(name="opencode", label="x", build=build)
    monkeypatch.setattr(clis, "plugin", lambda cli: bad)
    monkeypatch.setattr(process, "plugin", lambda cli: bad)
    events = [e async for e in runner.invoke(InvokeSpec(user_id=USER, cli="opencode", prompt="x"))]
    assert events[-1]["error"] == "invalid_request"
    assert not (tmp_path / "outside.txt").exists()
    assert json.dumps(events)
