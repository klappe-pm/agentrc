"""Accounting tests for session-card.py: usage counted once per API turn.

Each case is a regression found against real transcripts on 2026-09-23. The
runtime writes one assistant record per content block of a single API turn,
and every one of those records repeats the turn's full usage. Summing per
record inflated every total about 2.5 times on real data (1230 assistant
records held 493 distinct message ids). The shell suite beside this file covers
the card's text fields; these cover its numbers.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[3] / "stratarc" / "data" / "hooks" / "lib"
SPEC = importlib.util.spec_from_file_location("_session_card_accounting", HERE / "session-card.py")
assert SPEC and SPEC.loader
card_lib = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = card_lib
SPEC.loader.exec_module(card_lib)

SID = "aaaaaaaa-1111-2222-3333-444444444444"
USAGE = {"input_tokens": 10, "output_tokens": 100, "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 500}
TURN = 1610


def user(text, uuid="u1"):
    return {"type": "user", "uuid": uuid, "sessionId": SID, "cwd": "/tmp/demo", "timestamp": "2026-09-23T10:00:00Z", "message": {"role": "user", "content": text}}


def assistant(message_id, uuid, usage=USAGE, content=None, request_id=None):
    record = {
        "type": "assistant",
        "uuid": uuid,
        "sessionId": SID,
        "timestamp": "2026-09-23T10:00:01Z",
        "message": {"id": message_id, "role": "assistant", "model": "claude-opus-5", "usage": usage, "content": content or [{"type": "text", "text": "ok"}]},
    }
    if request_id:
        record["requestId"] = request_id
    return record


def write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return path


class AccountingCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "-tmp-demo"

    def tearDown(self):
        self.tmp.cleanup()

    def session(self, rows):
        return write(self.root / f"{SID}.jsonl", rows)


class TestOncePerTurn(AccountingCase):
    def test_records_sharing_a_message_id_count_once(self):
        path = self.session(
            [
                user("do the thing"),
                assistant("msg_1", "a1", content=[{"type": "thinking", "thinking": "..."}]),
                assistant("msg_1", "a2", content=[{"type": "tool_use", "id": "toolu_A", "name": "Read", "input": {}}]),
            ]
        )
        self.assertEqual(card_lib.build_card(path).tokens, TURN)

    def test_distinct_turns_still_add_up(self):
        path = self.session([user("one"), assistant("msg_1", "a1"), assistant("msg_2", "a2")])
        self.assertEqual(card_lib.build_card(path).tokens, 2 * TURN)

    def test_request_id_then_uuid_key_a_turn_without_a_message_id(self):
        no_id = [
            user("one"),
            assistant(None, "a1", request_id="req_1"),
            assistant(None, "a2", request_id="req_1"),
            assistant(None, "a3"),
        ]
        path = self.session(no_id)
        self.assertEqual(card_lib.build_card(path).tokens, 2 * TURN)

    def test_the_last_usage_of_a_turn_wins(self):
        # Streaming can write a partial output count before the final one.
        partial = dict(USAGE, output_tokens=1)
        path = self.session([user("one"), assistant("msg_1", "a1", usage=partial), assistant("msg_1", "a2")])
        self.assertEqual(card_lib.build_card(path).tokens, TURN)

    def test_thinking_tokens_count_once_per_turn(self):
        thinking = dict(USAGE, output_tokens_details={"thinking_tokens": 40})
        path = self.session([user("one"), assistant("msg_1", "a1", usage=thinking), assistant("msg_1", "a2", usage=thinking)])
        self.assertEqual(card_lib.build_card(path).thinking_tokens, 40)

    def test_usage_by_turn_is_shared_and_exact(self):
        path = self.session([user("one"), assistant("msg_1", "a1"), assistant("msg_1", "a2")])
        totals, thinking = card_lib.usage_by_turn(path)
        self.assertEqual(totals, {"in": 10, "out": 100, "cache_read": 1000, "cache_write": 500})
        self.assertEqual(thinking, 0)

    def test_records_with_no_identity_are_each_their_own_turn(self):
        # turn_key's last resort was id(record): once a parsed record is
        # freed its address is reused, so unrelated turns merged and the
        # count depended on memory layout (6 records counted as 5 turns).
        rows = [user("one")]
        for _ in range(6):
            bare = assistant(None, None)
            del bare["uuid"]
            rows.append(bare)
        path = self.session(rows)
        totals, _ = card_lib.usage_by_turn(path)
        self.assertEqual(totals["in"], 60)
        self.assertEqual(card_lib.usage_by_model(path)["claude-opus-5"]["in"], 60)

    def test_usage_by_model_splits_turns_and_sums_to_usage_by_turn(self):
        other = assistant("msg_2", "a3")
        other["message"]["model"] = "claude-sonnet-5"
        synthetic = assistant("msg_3", "a4", usage={"input_tokens": 0, "output_tokens": 0})
        synthetic["message"]["model"] = "<synthetic>"
        path = self.session([user("one"), assistant("msg_1", "a1"), assistant("msg_1", "a2"), other, synthetic])
        by_model = card_lib.usage_by_model(path)
        turn = {"in": 10, "out": 100, "cache_read": 1000, "cache_write": 500}
        self.assertEqual(by_model, {"claude-opus-5": turn, "claude-sonnet-5": turn})
        totals, _ = card_lib.usage_by_turn(path)
        self.assertEqual(totals, {key: 2 * value for key, value in turn.items()})
        nameless = assistant("msg_4", "a5")
        del nameless["message"]["model"]
        self.assertEqual(card_lib.usage_by_model(self.session([nameless]))["unknown"], turn)
        self.assertEqual(card_lib.usage_by_model(self.root / "missing.jsonl"), {})


class TestAgentsWithoutCostState(AccountingCase):
    def test_a_session_without_cost_state_includes_its_dispatched_agents(self):
        path = self.session([user("one"), assistant("msg_1", "a1")])
        write(self.root / SID / "subagents" / "agent-a1.jsonl", [assistant("msg_c", "c1"), assistant("msg_c", "c2")])
        self.assertEqual(card_lib.build_card(path).tokens, 2 * TURN)

    def test_a_session_with_cost_state_still_counts_its_agents_from_their_transcripts(self):
        cost = {"type": "cost-state", "sessionId": SID, "totalCostUSD": 1.0, "modelUsage": {"claude-opus-5": {"inputTokens": 1, "outputTokens": 1}}}
        path = self.session([user("one"), assistant("msg_1", "a1"), cost])
        write(self.root / SID / "subagents" / "agent-a1.jsonl", [assistant("msg_c", "c1")])
        # B-06: the session's turns plus its agent's turns, whatever cost-state says.
        self.assertEqual(card_lib.build_card(path).tokens, 2 * TURN)

    def test_an_agent_card_counts_only_its_own_transcript(self):
        write(self.root / f"{SID}.jsonl", [user("one"), assistant("msg_1", "a1")])
        agent = write(self.root / SID / "subagents" / "agent-a1.jsonl", [assistant("msg_c", "c1"), assistant("msg_c", "c2")])
        self.assertEqual(card_lib.build_card(agent).tokens, TURN)


def load_budget_report():
    target = HERE.parents[2] / "scripts" / "budget-report.py"
    if not target.is_file():
        raise unittest.SkipTest("scripts/budget-report.py is not in this checkout")
    spec = importlib.util.spec_from_file_location("_budget_report_for_card", target)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class TestOneTokenFigure(AccountingCase):
    """B-06: tokens come from transcript turns in every view, never cost-state."""

    def cost(self, **extra):
        record = {
            "type": "cost-state",
            "sessionId": SID,
            "totalCostUSD": 2.0,
            "totalDuration": 60000,
            "modelUsage": {"claude-opus-5": {"inputTokens": 999, "outputTokens": 999, "cacheReadInputTokens": 99999, "cacheCreationInputTokens": 9999, "thinkingTokens": 77}},
        }
        record.update(extra)
        return record

    def test_turns_win_over_a_disagreeing_cost_state_and_match_the_budget_report(self):
        path = self.session([user("one"), assistant("msg_1", "a1"), assistant("msg_2", "a2"), self.cost()])
        write(self.root / SID / "subagents" / "agent-a1.jsonl", [assistant("msg_c", "c1"), assistant("msg_c", "c2")])
        card = card_lib.build_card(path)
        self.assertEqual(card.tokens, 3 * TURN)
        measured = load_budget_report().measure_session(path)
        self.assertEqual(card.tokens, measured["measures"]["tokens.total"])
        # cost-state stays the source of dollars, duration and models.
        self.assertEqual(card.cost_usd, 2.0)
        self.assertEqual(card.duration_ms, 60000)
        self.assertEqual(card.models, ["opus"])

    def test_thinking_comes_from_turns_when_turns_report_it(self):
        thinking = dict(USAGE, output_tokens_details={"thinking_tokens": 40})
        path = self.session([user("one"), assistant("msg_1", "a1", usage=thinking), self.cost()])
        self.assertEqual(card_lib.build_card(path).thinking_tokens, 40)

    def test_thinking_falls_back_to_cost_state_when_turns_report_none(self):
        path = self.session([user("one"), assistant("msg_1", "a1"), self.cost()])
        self.assertEqual(card_lib.build_card(path).thinking_tokens, 77)

    def test_cost_as_of_is_the_last_timestamp_before_the_last_cost_state(self):
        early = dict(assistant("msg_1", "a1"), timestamp="2026-09-23T10:05:00Z")
        later = dict(assistant("msg_2", "a2"), timestamp="2026-09-23T11:30:00Z")
        path = self.session([user("one"), early, self.cost(totalCostUSD=1.0), later])
        card = card_lib.build_card(path)
        self.assertEqual(card.cost_as_of.isoformat(), "2026-09-23T10:05:00+00:00")
        self.assertEqual(card.to_dict()["cost_as_of"], "2026-09-23T10:05:00+00:00")
        self.assertEqual(card.last_active.isoformat(), "2026-09-23T11:30:00+00:00")

    def test_a_second_cost_state_moves_cost_as_of(self):
        early = dict(assistant("msg_1", "a1"), timestamp="2026-09-23T10:05:00Z")
        later = dict(assistant("msg_2", "a2"), timestamp="2026-09-23T11:30:00Z")
        path = self.session([user("one"), early, self.cost(), later, self.cost()])
        self.assertEqual(card_lib.build_card(path).cost_as_of.isoformat(), "2026-09-23T11:30:00+00:00")

    def test_a_system_record_before_the_cost_state_dates_it(self):
        """Codex review on PR 46: records the type filter skips still carry
        timestamps, and the last one before cost-state is the right date."""
        early = dict(assistant("msg_1", "a1"), timestamp="2026-09-23T10:00:00Z")
        system = {"type": "system", "subtype": "turn_duration", "sessionId": SID, "timestamp": "2026-09-23T10:07:00Z", "durationMs": 420000}
        path = self.session([user("one"), early, system, self.cost()])
        self.assertEqual(card_lib.build_card(path).cost_as_of.isoformat(), "2026-09-23T10:07:00+00:00")

    def test_a_typed_record_after_a_system_record_still_wins(self):
        system = {"type": "system", "subtype": "turn_duration", "sessionId": SID, "timestamp": "2026-09-23T10:07:00Z"}
        later = dict(assistant("msg_1", "a1"), timestamp="2026-09-23T10:09:00Z")
        path = self.session([user("one"), system, later, self.cost()])
        self.assertEqual(card_lib.build_card(path).cost_as_of.isoformat(), "2026-09-23T10:09:00+00:00")

    def test_no_cost_state_means_no_cost_as_of(self):
        path = self.session([user("one"), assistant("msg_1", "a1")])
        card = card_lib.build_card(path)
        self.assertIsNone(card.cost_as_of)
        self.assertIsNone(card.to_dict()["cost_as_of"])


class TestMalformedFields(AccountingCase):
    def test_a_message_that_is_not_an_object_is_skipped(self):
        bad = {"type": "assistant", "uuid": "a9", "sessionId": SID, "message": "oops"}
        path = self.session([user("one"), bad, assistant("msg_1", "a1")])
        self.assertEqual(card_lib.build_card(path).tokens, TURN)

    def test_a_non_numeric_cost_reads_as_unrecorded_numbers_not_a_crash(self):
        cost = {"type": "cost-state", "sessionId": SID, "totalCostUSD": "n/a", "totalDuration": "x", "modelUsage": []}
        card = card_lib.build_card(self.session([user("one"), assistant("msg_1", "a1"), cost]))
        self.assertEqual(card.cost_usd, 0.0)
        self.assertEqual(card.duration_ms, 0)
        self.assertEqual(card.tokens, TURN)

    def test_numeric_strings_and_floats_count_the_same_as_integers(self):
        odd = {"input_tokens": "10", "output_tokens": 100.0, "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 500}
        self.assertEqual(card_lib.build_card(self.session([user("one"), assistant("msg_1", "a1", usage=odd)])).tokens, TURN)

    def test_one_bad_transcript_does_not_stop_a_scan(self):
        write(self.root / "bbbbbbbb-1111-2222-3333-444444444444.jsonl", [user("fine"), assistant("msg_1", "a1")])
        write(self.root / f"{SID}.jsonl", [user("one"), {"type": "cost-state", "sessionId": SID, "totalCostUSD": "n/a"}])
        cards = card_lib.scan(Path(self.tmp.name))
        self.assertEqual(len(cards), 2)


class TestSessionsWithoutAPrompt(AccountingCase):
    def test_a_session_that_spent_tokens_stays_in_the_scan(self):
        # Opened by a slash command: no prompt the card counts as a turn, but
        # real spend, so the picker must still list it.
        self.session([user("<command-name>/code-review</command-name>"), assistant("msg_1", "a1")])
        cards = card_lib.scan(Path(self.tmp.name))
        self.assertEqual([c.tokens for c in cards], [TURN])


if __name__ == "__main__":
    unittest.main()
