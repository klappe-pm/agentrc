"""Tests for hooks/lib/runtime-usage.py, the token readers for runtimes other than Claude Code.

The Codex fixtures under fixtures/runtime-usage/codex/ were captured on
2026-09-23 from real rollouts on the operator's Mac with this module's own
capture command, which keeps only the records the reader reads and runs every
remaining string through the repository's detector:

- 01a0d02c-ca3f-7030-9935-5ec6bec65c49, a plain `codex exec` whose PreToolUse
  payload wrote tool-budget state under that id: its own rollout carries the
  tokens.
- 01a0d01c-c49b-7a31-bbef-7068bdfea8ac, a `codex exec review`: its own rollout
  carries no token_count, and the review agent's rollout
  (01a0d01c-c590-78e3-9041-dc5653d899f5) names it in session_meta.session_id.

Expected numbers are the fixtures' own last total_token_usage records.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
LIB = HERE.parents[2] / "agentrc" / "data" / "hooks" / "lib"
FIXTURE_HOME = HERE / "fixtures" / "runtime-usage" / "codex"
DAY = FIXTURE_HOME / "sessions" / "2026" / "09" / "23"

LIVE = "01a0d02c-ca3f-7030-9935-5ec6bec65c49"
REVIEW = "01a0d01c-c49b-7a31-bbef-7068bdfea8ac"
REVIEW_AGENT = "01a0d01c-c590-78e3-9041-dc5653d899f5"


def load():
    spec = importlib.util.spec_from_file_location("_runtime_usage_under_test", LIB / "runtime-usage.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


usage = load()


def copy_home(names):
    """A scratch Codex home holding only the named fixture rollouts."""
    tmp = tempfile.TemporaryDirectory()
    day = Path(tmp.name) / "sessions" / "2026" / "09" / "23"
    day.mkdir(parents=True)
    for path in DAY.glob("rollout-*.jsonl"):
        if any(name in path.name for name in names):
            shutil.copy(path, day / path.name)
    return tmp, Path(tmp.name), day


class TestCodexReader(unittest.TestCase):
    def test_a_plain_exec_session_reads_its_own_rollout(self):
        result = usage.measure("codex", LIVE, FIXTURE_HOME)
        self.assertTrue(result["measured"], result)
        measures = result["measures"]
        # input_tokens 71953 includes the 42752 cached: 29201 uncached.
        self.assertEqual(measures["tokens.input"], 29201)
        self.assertEqual(measures["tokens.cache_read"], 42752)
        self.assertEqual(measures["tokens.output"], 80)
        self.assertEqual(measures["tokens.cache_write"], 0)
        # The kinds sum to Codex's own total_tokens.
        self.assertEqual(measures["tokens.total"], 72033)
        self.assertEqual(measures["turns"], 1)
        self.assertEqual(measures["wall_minutes"], 0.2)
        self.assertEqual(result["agents"], 0)
        self.assertEqual(result["models"], ["gpt-6-astra"])
        self.assertEqual(len(result["files"]), 1)

    def test_a_review_session_counts_the_agent_rollout_that_names_it(self):
        result = usage.measure("codex", REVIEW, FIXTURE_HOME)
        self.assertTrue(result["measured"], result)
        measures = result["measures"]
        self.assertEqual(measures["tokens.input"], 601561 - 545024)
        self.assertEqual(measures["tokens.cache_read"], 545024)
        self.assertEqual(measures["tokens.output"], 3345)
        self.assertEqual(measures["tokens.total"], 604906)
        self.assertEqual(result["agents"], 1)
        # Turns are the session's own; the agent's turns were not typed.
        self.assertEqual(measures["turns"], 1)
        self.assertEqual(measures["wall_minutes"], 1.6)
        self.assertEqual(result["branch"], "plan/host-bootstrap-credential")
        self.assertTrue(any(REVIEW_AGENT in path for path in result["files"]))

    def test_the_agent_rollout_alone_is_not_a_session(self):
        # The payload never names the agent's thread id, so no rollout is named by it.
        result = usage.measure("codex", REVIEW_AGENT, FIXTURE_HOME)
        self.assertFalse(result["measured"])
        self.assertIn("no rollout", result["reason"])

    def test_an_unreported_kind_is_absent_never_zero(self):
        tmp, home, day = copy_home([LIVE])
        try:
            path = next(day.glob("rollout-*.jsonl"))
            lines = []
            for line in path.read_text(encoding="utf-8").splitlines():
                record = json.loads(line)
                info = (record.get("payload") or {}).get("info")
                if isinstance(info, dict):
                    # A modified copy of the captured record, with one kind removed.
                    info["total_token_usage"].pop("cache_write_input_tokens")
                lines.append(json.dumps(record))
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            result = usage.measure("codex", LIVE, home)
        finally:
            tmp.cleanup()
        self.assertTrue(result["measured"], result)
        self.assertNotIn("tokens.cache_write", result["measures"])
        self.assertEqual(result["measures"]["tokens.total"], 72033)

    def test_a_rollout_with_no_token_record_is_not_measured(self):
        # The review session's own rollout alone, without the agent rollout that carries the tokens.
        tmp, home, day = copy_home([REVIEW])
        try:
            self.assertEqual([path.name for path in day.iterdir()], [f"rollout-2026-09-23T14-12-21-{REVIEW}.jsonl"])
            result = usage.measure("codex", REVIEW, home)
        finally:
            tmp.cleanup()
        self.assertFalse(result["measured"])
        self.assertIn("no token_count", result["reason"])
        self.assertEqual(result["measures"], {})

    def test_an_agent_whose_session_id_follows_a_long_field_is_still_found(self):
        """Codex review of PR 81: a 4096 character head missed a session_id placed after a large field."""
        tmp, home, day = copy_home([REVIEW, REVIEW_AGENT])
        try:
            agent = next(day.glob(f"*{REVIEW_AGENT}*"))
            lines = agent.read_text(encoding="utf-8").splitlines()
            meta = json.loads(lines[0])
            payload = meta["payload"]
            # The same record with a 5000 character field ahead of session_id.
            meta["payload"] = {"base_instructions": "x" * 5000, **{k: v for k, v in payload.items()}}
            agent.write_text("\n".join([json.dumps(meta)] + lines[1:]) + "\n", encoding="utf-8")
            result = usage.measure("codex", REVIEW, home)
        finally:
            tmp.cleanup()
        self.assertTrue(result["measured"], result)
        self.assertEqual(result["agents"], 1)
        self.assertEqual(result["measures"]["tokens.total"], 604906)

    def test_an_unknown_session_is_not_measured(self):
        result = usage.measure("codex", "01a0d0ff-0000-7000-8000-000000000000", FIXTURE_HOME)
        self.assertFalse(result["measured"])
        self.assertIn("no rollout", result["reason"])

    def test_a_path_shaped_id_is_refused(self):
        result = usage.measure("codex", "../../etc", FIXTURE_HOME)
        self.assertFalse(result["measured"])


class TestRolloutByCwd(unittest.TestCase):
    """WI-93: a codex call that named no session id is matched to its rollout by cwd and start time."""

    CWD = "/home/user/projects/_worktrees/example/plan-host-credential"

    def stamp(self, text):
        return usage._parse_ts(text)

    def find(self, since, **kwargs):
        return usage.codex_rollout_by_cwd(self.CWD, self.stamp(since), FIXTURE_HOME, until=self.stamp("2026-09-24T00:00:00Z"), **kwargs)

    def test_the_session_own_rollout_is_found_never_its_agent(self):
        # The review agent's rollout shares the cwd but carries its parent's session_id.
        self.assertEqual(self.find("2026-09-23T21:12:00Z"), REVIEW)

    def test_a_rollout_started_before_the_call_is_not_the_call(self):
        self.assertIsNone(self.find("2026-09-23T21:12:30Z"))

    def test_concurrent_sessions_in_the_same_cwd_are_unresolved(self):
        tmp, home, day = copy_home([REVIEW])
        other = "01a0d01c-c49b-7a31-bbef-7068bdfea8ad"
        try:
            source = next(day.glob(f"rollout-*-{REVIEW}.jsonl"))
            row = json.loads(source.read_text(encoding="utf-8").splitlines()[0])
            row["payload"]["id"] = other
            row["payload"]["session_id"] = other
            (day / f"rollout-2026-09-23T14-12-22-{other}.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
            found = usage.codex_rollout_by_cwd(self.CWD, self.stamp("2026-09-23T21:12:00Z"), home, until=self.stamp("2026-09-24T00:00:00Z"))
            self.assertIsNone(found)
        finally:
            tmp.cleanup()

    def test_a_claimed_rollout_is_skipped_and_the_agent_is_still_not_chosen(self):
        self.assertIsNone(self.find("2026-09-23T21:12:00Z", exclude=[REVIEW]))

    def test_another_directory_is_not_a_match(self):
        found = usage.codex_rollout_by_cwd("/tmp/elsewhere", self.stamp("2026-09-23T00:00:00Z"), FIXTURE_HOME, until=self.stamp("2026-09-24T00:00:00Z"))
        self.assertIsNone(found)

    def test_a_rollout_after_the_window_is_not_a_match(self):
        found = usage.codex_rollout_by_cwd(self.CWD, self.stamp("2026-09-23T20:00:00Z"), FIXTURE_HOME, until=self.stamp("2026-09-23T21:00:00Z"))
        self.assertIsNone(found)


class TestUnconfirmedRuntimes(unittest.TestCase):
    def test_runtimes_without_three_confirmed_facts_stay_na(self):
        for runtime in ("gemini", "opencode", "cursor"):
            result = usage.measure(runtime, "any-session")
            self.assertFalse(result["measured"], runtime)
            self.assertEqual(result["measures"], {})
            self.assertIn("no reader", result["reason"])

    def test_codex_is_the_one_reader(self):
        self.assertEqual(sorted(usage.READERS), ["codex"])


class TestCapture(unittest.TestCase):
    def test_capture_keeps_counts_drops_text_and_redacts_token_shaped_values(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            home = Path(tmp.name) / "home"
            day = home / "sessions" / "2026" / "09" / "23"
            day.mkdir(parents=True)
            session = "01a0d0aa-0000-7000-8000-000000000001"
            # Built at run time so no token-shaped literal is committed.
            planted = "ghp_" + "A1b2C3d4E5" * 4
            rows = [
                {
                    "timestamp": "2026-09-23T10:00:00.000Z",
                    "type": "session_meta",
                    "payload": {
                        "session_id": session,
                        "id": session,
                        "cwd": f"/tmp/{planted}",
                        "base_instructions": {"text": "long instructions"},
                        "git": {"branch": "wi-37-x", "repository_url": "https://example.invalid/r.git"},
                    },
                },
                {
                    "timestamp": "2026-09-23T10:00:01.000Z",
                    "type": "response_item",
                    "payload": {"type": "message", "role": "user", "content": [{"text": "secret prompt"}]},
                },
                {
                    "timestamp": "2026-09-23T10:00:02.000Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {"total_token_usage": {"input_tokens": 10, "cached_input_tokens": 4, "output_tokens": 2}},
                        "rate_limits": {"plan_type": "pro"},
                    },
                },
            ]
            source = day / f"rollout-2026-09-23T03-00-00-{session}.jsonl"
            source.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            out = Path(tmp.name) / "out"
            written = usage.capture_codex(session, out, home)
            self.assertEqual(len(written), 1)
            text = written[0].read_text(encoding="utf-8")
            self.assertEqual(written[0].relative_to(out).as_posix(), source.relative_to(home).as_posix())
            self.assertNotIn(planted, text)
            self.assertIn("[REDACTED:", text)
            for dropped in ("long instructions", "secret prompt", "repository_url", "plan_type"):
                self.assertNotIn(dropped, text)
            self.assertIn("wi-37-x", text)
            # The capture still reads as the same session.
            result = usage.measure("codex", session, out)
            self.assertEqual(result["measures"]["tokens.total"], 12)
            self.assertNotIn("tokens.cache_write", result["measures"])
        finally:
            tmp.cleanup()

    def test_a_value_json_escaping_hides_from_the_detector_is_still_redacted(self):
        """Codex review of PR 81: a tab inside a value matched raw but not once serialized as \\t."""
        tmp = tempfile.TemporaryDirectory()
        try:
            home = Path(tmp.name) / "home"
            day = home / "sessions" / "2026" / "09" / "23"
            day.mkdir(parents=True)
            session = "01a0d0aa-0000-7000-8000-000000000002"
            planted = "Bearer" + "\t" + "Ab12" * 8
            self.assertIsNotNone(usage.detector_label(planted))
            self.assertIsNone(usage.detector_label(json.dumps({"cwd": planted})))
            row = {
                "timestamp": "2026-09-23T10:00:00.000Z",
                "type": "session_meta",
                "payload": {"session_id": session, "id": session, "cwd": planted},
            }
            (day / f"rollout-2026-09-23T03-00-00-{session}.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
            written = usage.capture_codex(session, Path(tmp.name) / "out", home)
            captured = json.loads(written[0].read_text(encoding="utf-8").splitlines()[0])
        finally:
            tmp.cleanup()
        self.assertTrue(captured["payload"]["cwd"].startswith("[REDACTED:"), captured)

    def test_a_detector_that_cannot_run_stops_the_capture(self):
        """Codex review of PR 81: a missing detector returned None, so capture wrote unscanned content."""
        tmp = tempfile.TemporaryDirectory()
        try:
            out = Path(tmp.name) / "out"
            with mock.patch.object(usage, "DETECTOR", Path(tmp.name) / "missing-detector.sh"):
                with self.assertRaises(SystemExit):
                    usage.detector_label("anything")
                with self.assertRaises(SystemExit):
                    usage.capture_codex(LIVE, out, FIXTURE_HOME)
            self.assertEqual(list(out.rglob("*.jsonl")) if out.exists() else [], [])
        finally:
            tmp.cleanup()

    def test_a_detector_that_fails_is_not_a_clean_scan(self):
        """Only exit 1 means no match; a detector that errors (exit 2 here) stops the capture."""
        tmp = tempfile.TemporaryDirectory()
        try:
            broken = Path(tmp.name) / "broken-detector.sh"
            broken.write_text("exit 2\n", encoding="utf-8")
            with mock.patch.object(usage, "DETECTOR", broken):
                with self.assertRaises(SystemExit):
                    usage.detector_label("anything")
        finally:
            tmp.cleanup()

    def test_committed_fixtures_carry_no_token_shaped_value(self):
        def strings(node):
            if isinstance(node, dict):
                for value in node.values():
                    yield from strings(value)
            elif isinstance(node, list):
                for value in node:
                    yield from strings(value)
            elif isinstance(node, str):
                yield node

        for path in sorted(FIXTURE_HOME.rglob("*.jsonl")):
            text = path.read_text(encoding="utf-8")
            self.assertIsNone(usage.detector_label(text), path)
            # Each decoded string too, since JSON escaping can hide a value.
            decoded = {value for line in text.splitlines() for value in strings(json.loads(line))}
            for value in sorted(decoded):
                self.assertIsNone(usage.detector_label(value), (path.name, value))


class TestCli(unittest.TestCase):
    def test_measure_json_and_na_exit(self):
        import contextlib
        import io

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = usage.main(["measure", "codex", LIVE, "--home", str(FIXTURE_HOME), "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["measures"]["tokens.total"], 72033)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = usage.main(["measure", "gemini", "x"])
        self.assertEqual(code, 1)
        self.assertIn("tokens n/a", out.getvalue())


if __name__ == "__main__":
    unittest.main()
