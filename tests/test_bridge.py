# Conversation-bridge behaviour: the proxy keeps a server-side
# conversation per client history and sends only the delta when
# the new request extends the known history by exactly one user
# turn; anything else falls back to a fresh stuffed conversation.
import pytest

import mai_proxy as mp


@pytest.fixture
def bridge(monkeypatch):
    mp._sessions.clear()
    calls = {"create": 0, "turns": []}

    def fake_trpc(name, payload, idempotent=False):
        assert name == "conversations.create"
        calls["create"] += 1
        return {"id": f"cid-{calls['create']}"}

    def fake_run_turn(cid, last, text, extra):
        calls["turns"].append({"cid": cid, "last": last, "text": text,
                                "extra": dict(extra or {})})
        return (f"reply-{len(calls['turns'])}",
                f"mid-{len(calls['turns'])}")

    monkeypatch.setattr(mp, "trpc", fake_trpc)
    monkeypatch.setattr(mp, "run_turn", fake_run_turn)
    monkeypatch.setattr(mp, "_save_sessions", lambda: None)
    yield calls
    mp._sessions.clear()


def test_first_call_stuffs_and_stores(bridge):
    out = mp._complete_inner("sys", [("user", "hi")], None, {})
    assert out == "reply-1"
    assert bridge["create"] == 1
    first = bridge["turns"][0]
    assert first["last"] is None
    assert first["extra"].get("isFirstMessage") is True
    assert "hi" in first["text"]


def test_prefix_extension_sends_only_the_delta(bridge):
    mp._complete_inner("sys", [("user", "hi")], None, {})
    out = mp._complete_inner(
        "sys",
        [("user", "hi"), ("assistant", "reply-1"), ("user", "next")],
        None, {})
    assert out == "reply-2"
    assert bridge["create"] == 1  # no new conversation
    delta = bridge["turns"][1]
    assert delta["cid"] == "cid-1"
    assert delta["last"] == "mid-1"
    assert delta["text"] == "next"  # ONLY the new turn went upstream


def test_two_new_user_turns_fall_back(bridge):
    mp._complete_inner("sys", [("user", "hi")], None, {})
    mp._complete_inner(
        "sys",
        [("user", "hi"), ("assistant", "reply-1"),
         ("user", "a"), ("user", "b")],
        None, {})
    assert bridge["create"] == 2  # restuffed into a fresh conversation


def test_system_change_falls_back(bridge):
    mp._complete_inner("sys", [("user", "hi")], None, {})
    mp._complete_inner(
        "sys-CHANGED",
        [("user", "hi"), ("assistant", "reply-1"), ("user", "next")],
        None, {})
    assert bridge["create"] == 2


def test_tool_result_can_be_the_delta(bridge):
    mp._complete_inner("sys", [("user", "hi")], None, {})
    mp._complete_inner(
        "sys",
        [("user", "hi"), ("assistant", "reply-1"),
         ("tool_result", "file contents")],
        None, {})
    assert bridge["create"] == 1
    assert "file contents" in bridge["turns"][1]["text"]


def test_failed_delta_drops_the_bridge(bridge, monkeypatch):
    mp._complete_inner("sys", [("user", "hi")], None, {})
    assert mp._sessions  # bridge exists

    def boom(cid, last, text, extra):
        raise mp.UpstreamError(500, "boom")

    monkeypatch.setattr(mp, "run_turn", boom)
    with pytest.raises(mp.UpstreamError):
        mp._complete_inner(
            "sys",
            [("user", "hi"), ("assistant", "reply-1"),
             ("user", "next")],
            None, {})
    assert mp._sessions == {}  # bridge dropped, next attempt restuffs
