# The prompt-emulated tool protocol and the streaming holdback
# filter that keeps a tool block from leaking into streamed prose.
import mai_proxy as mp

CALL = '<mai_tool_call>{"name": "Write", "input": {"path": "x"}}</mai_tool_call>'


def run_filter(pieces):
    f = mp.ToolBlockFilter()
    out = [f.feed(p) for p in pieces]
    return f, "".join(out)


class TestToolBlockFilter:
    def test_plain_text_passes_through(self):
        f, out = run_filter(["Hello ", "world"])
        assert out == "Hello world"
        assert f.full_text == "Hello world"

    def test_block_is_held_back(self):
        f, out = run_filter(["Answer. ", CALL])
        assert out == "Answer. "
        before, call = mp.parse_tool_call(f.full_text)
        assert before == "Answer."
        assert call["name"] == "Write"
        assert call["input"] == {"path": "x"}

    def test_tag_split_across_deltas(self):
        f, out = run_filter(["Ans", "wer. <mai_to", "ol_call>{\"name\": "
                             "\"Read\", \"input\": {}}"
                             "</mai_tool_call>"])
        assert out == "Answer. "
        before, call = mp.parse_tool_call(f.full_text)
        assert before == "Answer."
        assert call["name"] == "Read"

    def test_partial_tag_that_isnt_a_call_stays_text(self):
        # A trailing "<mai_to" might just be prose; it is held while
        # streaming but must come back as text when no block follows.
        f, out = run_filter(["see <mai_to"])
        assert out == "see "
        before, call = mp.parse_tool_call(f.full_text)
        assert call is None
        assert f.full_text == "see <mai_to"

    def test_emitted_is_always_a_prefix_of_before(self):
        pieces = ["The result ", "is <mai_tool_call>{\"name\": \"Bash\", "
                  "\"input\": {\"cmd\": \"ls\"}}</mai_tool_call>"]
        f, out = run_filter(pieces)
        before, call = mp.parse_tool_call(f.full_text)
        # parse_tool_call strips the prose; the streamed text may
        # carry one trailing space the buffered path would strip.
        assert out.rstrip() == before
        assert call["name"] == "Bash"


class TestParseToolCall:
    def test_no_block(self):
        assert mp.parse_tool_call("just text") == ("just text", None)

    def test_malformed_json_is_not_a_call(self):
        before, call = mp.parse_tool_call(
            'pre <mai_tool_call>{not json}</mai_tool_call>')
        assert call is None

    def test_first_block_wins(self):
        text = ('<mai_tool_call>{"name": "A", "input": {}}</mai_tool_call>'
                '<mai_tool_call>{"name": "B", "input": {}}</mai_tool_call>')
        _, call = mp.parse_tool_call(text)
        assert call["name"] == "A"


class TestTurnConversion:
    def test_openai_tool_round_trip(self):
        msgs = [
            {"role": "user", "content": "do it"},
            {"role": "assistant", "content": None,
             "tool_calls": [{"id": "call_1", "type": "function",
                             "function": {"name": "Write",
                                          "arguments": "{\"path\": \"x\"}"}}]},
            {"role": "tool", "tool_call_id": "call_1", "content": "done"},
            {"role": "user", "content": "thanks"},
        ]
        _, turns = mp.turns_from_openai(msgs)
        roles = [t[0] for t in turns]
        assert roles == ["user", "assistant_tool", "tool_result", "user"]

    def test_anthropic_system_and_turns(self):
        msgs = [{"role": "user",
                 "content": [{"type": "text", "text": "hi"}]}]
        system, turns = mp.turns_from_anthropic("be nice", msgs)
        assert system == "be nice"
        assert turns == [("user", "hi")]
