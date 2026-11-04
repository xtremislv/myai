"""Long conversations are fitted into the model's context window instead of failing."""
from __future__ import annotations

from app.llm.client import fit_to_context


def _turn(i, size):
    return [{"role": "user", "content": f"q{i} " + "x" * size},
            {"role": "assistant", "content": "", "tool_calls": [{"id": f"c{i}"}]},
            {"role": "tool", "tool_call_id": f"c{i}", "content": "y" * size},
            {"role": "assistant", "content": f"a{i}"}]


def test_a_short_chat_goes_in_whole():
    msgs = [{"role": "system", "content": "s"}, *_turn(1, 100)]
    assert fit_to_context(msgs, None, 1000, context=8000) == (msgs, 1000)


def test_oldest_turns_go_first_and_what_is_left_starts_on_a_user_turn():
    msgs = [{"role": "system", "content": "rules"}]
    for i in range(10):
        msgs += _turn(i, 3000)
    kept, reply = fit_to_context(msgs, None, 1000, context=8000)
    assert kept[0] == msgs[0] and kept[1]["role"] == "user"
    assert kept[-1] == msgs[-1] and reply == 1000
    assert len(kept) < len(msgs)
    assert not any(m["content"].startswith("q0") for m in kept)


def test_a_huge_last_turn_keeps_itself_and_shortens_the_reply():
    msgs = [{"role": "system", "content": "s"}, *_turn(0, 100), {"role": "user", "content": "z" * 20000}]
    kept, reply = fit_to_context(msgs, None, 4000, context=8000)
    assert kept == [msgs[0], msgs[-1]]
    assert 256 <= reply < 4000
