"""The council board is part of every seat's communication tools, whatever its role grants."""

from tests.test_council_comms import _council, db, run_row  # noqa: F401 (fixtures)
from tests.test_limit_extension import reply


async def test_every_seat_can_read_and_post_to_the_board(db, run_row):  # noqa: F811
    orch, ctx, _, llm, (_, _, cy) = _council(db, run_row, lambda req: reply("done"))
    # Cy's role grants only notes_read; the board's tools come with the council regardless.
    await orch.runner(cy).run_task("Write the summary", step_title="Draft")
    names = {t.name for t in llm.requests[-1].tools}
    assert {"notes_write", "notes_read"} <= names
    # The run's own deny list still wins.
    ctx.engine.rails.tools.deny = ["notes_write"]
    await orch.runner(cy).run_task("Write the summary", step_title="Draft again")
    assert "notes_write" not in {t.name for t in llm.requests[-1].tools}
