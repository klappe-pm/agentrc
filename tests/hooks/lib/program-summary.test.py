"""Tests for hooks/lib/program-summary.py, the program counts for the operator.

An interactive session is told, in one line, how many runs are in flight, how
many pull requests wait for the operator's review and how many items wait on
a decision. The counts come from `program-status.py snapshot --json` and
nothing else, so the end-to-end cases run the real script against a fixture
event log (PROGRAM_EVENTS_FILE), and the failure cases point LLM_ROOT at a
fake checkout whose program-status.py misbehaves.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

LIB = Path(__file__).resolve().parents[3] / "agentrc" / "data" / "hooks" / "lib"
HELPER = LIB / "program-summary.py"
REPO = Path(__file__).resolve().parents[3]
needs_status_script = unittest.skipUnless((REPO / "scripts" / "program-status.py").is_file(), "scripts/program-status.py is not in this checkout")


def load():
    spec = importlib.util.spec_from_file_location("_program_summary_under_test", HELPER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def stamp(hours_ago: float, now: dt.datetime | None = None) -> str:
    base = now or dt.datetime.now(dt.timezone.utc)
    return (base - dt.timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def event(item: str, name: str, hours_ago: float = 0.1, pr: int | None = None, lane: str = "autonomy", now: dt.datetime | None = None) -> dict:
    return {
        "contract_version": "1",
        "ts": stamp(hours_ago, now),
        "run": "fixture-run",
        "lane": lane,
        "item": item,
        "event": name,
        "pr": pr,
        "branch": None,
        "commit": None,
        "detail": "Fixture event.",
    }


def fixture_stream() -> list[dict]:
    """Three runs in flight, one pull request awaiting review, one item waiting on a decision."""
    return [
        event("WI-1", "item_started"),
        event("WI-2", "item_started", hours_ago=2),
        event("WI-2", "handoff_posted", pr=7),
        event("WI-3", "item_started", hours_ago=2),
        event("WI-3", "needs_decision"),
        event("WI-4", "pr_opened", hours_ago=1, pr=8),
        event("WI-4", "merged", pr=8),
        # Last heard from three days ago: not a run in flight any more.
        event("WI-5", "item_started", hours_ago=72),
        # A review finding about an item is not a step of its run.
        event("WI-6", "codex_review_posted", pr=9),
        event("WI-6", "finding", lane="review"),
        # Answered and picked up again by the next pass.
        event("WI-7", "needs_decision", hours_ago=3),
        event("WI-7", "item_started"),
        # The dispatcher's own pause and resume are not a work item.
        event("dispatcher", "paused", hours_ago=1),
        event("dispatcher", "resumed"),
    ]


def snapshot_of(events: list[dict], generated_at: str) -> dict:
    return {"contract_version": "1", "generated_at": generated_at, "events": {"items": events, "skipped": 0}}


class CountsTest(unittest.TestCase):
    """counts() over a snapshot document: the classification itself."""

    def setUp(self):
        self.module = load()
        self.now = dt.datetime(2026, 9, 24, 12, 0, tzinfo=dt.timezone.utc)
        self.generated = self.now.strftime("%Y-%m-%dT%H:%M:%SZ")

    def count(self, events):
        return self.module.counts(snapshot_of(events, self.generated))

    def test_the_fixture_stream(self):
        events = [dict(item, ts=stamp(hours, self.now)) for item, hours in zip(fixture_stream(), (0.1, 2, 0.1, 2, 0.1, 1, 0.1, 72, 0.1, 0.1, 3, 0.1, 1, 0.1))]
        self.assertEqual(self.count(events), {"runs": 3, "reviews": 1, "decisions": 1})

    def test_an_empty_stream_counts_nothing(self):
        self.assertEqual(self.count([]), {"runs": 0, "reviews": 0, "decisions": 0})

    def test_a_dispatched_item_is_in_flight(self):
        # dispatched is WI-60's event; it counts as soon as the schema admits it.
        self.assertEqual(self.count([event("WI-9", "dispatched", now=self.now)])["runs"], 1)

    def test_a_decision_and_a_review_do_not_age_out(self):
        events = [event("WI-1", "needs_decision", hours_ago=200, now=self.now), event("WI-2", "handoff_posted", hours_ago=200, pr=3, now=self.now)]
        self.assertEqual(self.count(events), {"runs": 0, "reviews": 1, "decisions": 1})

    def test_ended_runs_are_not_counted(self):
        for name in ("merged", "item_done", "run_failed", "blocked", "escalated", "paused"):
            with self.subTest(name=name):
                events = [event("WI-1", "item_started", hours_ago=1, now=self.now), event("WI-1", name, now=self.now)]
                self.assertEqual(self.count(events), {"runs": 0, "reviews": 0, "decisions": 0})

    def test_a_review_completed_report_does_not_hide_an_in_flight_run(self):
        # Mirrors finding and deployed (NOT_A_STEP): a background
        # review finishing after item_started must not become the item's latest
        # record and hide that the run is still going.
        events = [event("WI-1", "item_started", hours_ago=1, now=self.now), event("WI-1", "review_completed", lane="review", now=self.now)]
        self.assertEqual(self.count(events)["runs"], 1)

    def test_fractional_seconds_are_read(self):
        # The event schema allows them; fromisoformat on Python 3.9 rejects most.
        for fraction in (".1", ".12", ".1234", ".123456"):
            with self.subTest(fraction=fraction):
                record = event("WI-1", "item_started", now=self.now)
                record["ts"] = stamp(0.1, self.now)[:-1] + fraction + "Z"
                self.assertEqual(self.count([record])["runs"], 1)

    def test_a_malformed_snapshot_counts_nothing(self):
        for document in ({}, {"events": None}, {"events": {"items": "x"}}, {"events": {"items": [1, None, {"item": 5}]}, "generated_at": self.generated}):
            with self.subTest(document=document):
                self.assertEqual(self.module.counts(document), {"runs": 0, "reviews": 0, "decisions": 0})

    def test_the_sentence_and_the_segment(self):
        self.assertEqual(
            self.module.sentence({"runs": 3, "reviews": 1, "decisions": 2}),
            "Program: 3 runs in flight, 1 pull request awaiting your review, 2 items waiting on your decision.",
        )
        self.assertEqual(
            self.module.sentence({"runs": 1, "reviews": 2, "decisions": 1}),
            "Program: 1 run in flight, 2 pull requests awaiting your review, 1 item waiting on your decision.",
        )
        self.assertEqual(self.module.sentence({"runs": 0, "reviews": 0, "decisions": 0}), "")
        self.assertEqual(self.module.segment({"runs": 3, "reviews": 1, "decisions": 0}), "3 running · 1 to review · 0 to decide")
        self.assertEqual(self.module.segment({"runs": 0, "reviews": 0, "decisions": 0}), "")


class HelperTest(unittest.TestCase):
    """The helper as the hook and the status line run it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.events = self.dir / "program-events.jsonl"
        self.cache = self.dir / "state" / "program-summary.json"

    def tearDown(self):
        self.tmp.cleanup()

    def write_events(self, events: list[dict]):
        self.events.write_text("".join(json.dumps(item) + "\n" for item in events), encoding="utf-8")

    def env(self, **extra: str) -> dict:
        env = {key: value for key, value in os.environ.items() if not key.startswith(("PROGRAM_", "LLM_ROOT", "AGENTRC_SOURCE"))}
        env.update(
            {
                "HOME": str(self.dir / "home"),
                "LLM_ROOT": str(REPO),
                "PROGRAM_EVENTS_FILE": str(self.events),
                "PROGRAM_SUMMARY_CACHE": str(self.cache),
            }
        )
        env.update(extra)
        return env

    def run_helper(self, *args: str, **extra: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(HELPER), *args], capture_output=True, text=True, env=self.env(**extra), timeout=60)

    def fake_root(self, body: str) -> Path:
        root = self.dir / "fake-root"
        (root / "scripts").mkdir(parents=True, exist_ok=True)
        (root / "scripts" / "program-status.py").write_text(body, encoding="utf-8")
        return root

    def assert_silent(self, result: subprocess.CompletedProcess):
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")

    @needs_status_script
    def test_a_fixture_stream_produces_the_line(self):
        self.write_events(fixture_stream())
        result = self.run_helper("line")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout,
            "Program: 3 runs in flight, 1 pull request awaiting your review, 1 item waiting on your decision.\n",
        )
        cached = json.loads(self.cache.read_text(encoding="utf-8"))
        self.assertEqual(cached["counts"], {"runs": 3, "reviews": 1, "decisions": 1})

    @needs_status_script
    def test_an_empty_stream_prints_nothing(self):
        self.write_events([])
        self.assert_silent(self.run_helper("line"))
        self.assertEqual(json.loads(self.cache.read_text(encoding="utf-8"))["counts"], {"runs": 0, "reviews": 0, "decisions": 0})

    def test_a_missing_stream_prints_nothing(self):
        self.assert_silent(self.run_helper("line"))

    @needs_status_script
    def test_the_segment_reads_the_cache_the_line_wrote(self):
        self.write_events(fixture_stream())
        self.run_helper("line")
        # The status line never runs the snapshot: an unusable root proves it.
        result = self.run_helper("segment", LLM_ROOT=str(self.dir / "nowhere"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "3 running · 1 to review · 1 to decide\n")

    def test_the_segment_without_a_cache_prints_nothing(self):
        self.assert_silent(self.run_helper("segment", LLM_ROOT=str(self.dir / "nowhere")))

    @needs_status_script
    def test_a_stale_cache_is_refreshed_in_the_background(self):
        self.cache.parent.mkdir(parents=True)
        old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.cache.write_text(json.dumps({"generated_at": old, "counts": {"runs": 9, "reviews": 9, "decisions": 9}}), encoding="utf-8")
        self.write_events([event("WI-1", "needs_decision")])
        started = time.monotonic()
        result = self.run_helper("segment")
        self.assertLess(time.monotonic() - started, 5)
        # Too old to show, so nothing now; the refresh lands for a later frame.
        self.assert_silent(result)
        deadline = time.monotonic() + 30
        counts = None
        while time.monotonic() < deadline:
            try:
                counts = json.loads(self.cache.read_text(encoding="utf-8"))["counts"]
            except (OSError, ValueError):
                counts = None
            if counts == {"runs": 0, "reviews": 0, "decisions": 1}:
                break
            time.sleep(0.2)
        self.assertEqual(counts, {"runs": 0, "reviews": 0, "decisions": 1})
        self.assertEqual(self.run_helper("segment").stdout, "0 running · 0 to review · 1 to decide\n")

    def stale_cache(self):
        self.cache.parent.mkdir(parents=True, exist_ok=True)
        old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.cache.write_text(json.dumps({"generated_at": old, "counts": {"runs": 9, "reviews": 9, "decisions": 9}}), encoding="utf-8")

    def logging_root(self, tail: str) -> tuple[Path, Path]:
        """A fake checkout whose snapshot appends one line to calls.log per run, then runs tail."""
        calls = self.dir / "calls.log"
        body = f"from pathlib import Path\nwith Path({str(calls)!r}).open('a') as handle:\n    handle.write('call\\n')\n{tail}"
        return self.fake_root(body), calls

    def calls_after(self, calls: Path, settle: float = 2.0) -> int:
        time.sleep(settle)
        return len(calls.read_text().splitlines()) if calls.exists() else 0

    def test_a_frame_never_waits_for_the_snapshot(self):
        self.stale_cache()
        root, _calls = self.logging_root("import time\ntime.sleep(30)\n")
        started = time.monotonic()
        result = self.run_helper("segment", LLM_ROOT=str(root))
        self.assertLess(time.monotonic() - started, 1.5)
        self.assert_silent(result)

    def test_a_failing_refresh_is_not_retried_on_every_frame(self):
        root, calls = self.logging_root("import sys\nsys.exit(3)\n")
        for _ in range(4):
            self.assert_silent(self.run_helper("segment", LLM_ROOT=str(root)))
            time.sleep(0.5)
        self.assertEqual(self.calls_after(calls), 1)

    def test_a_held_lock_stops_a_second_refresh(self):
        self.stale_cache()
        root, calls = self.logging_root("import sys\nsys.exit(3)\n")
        lock = self.cache.with_name(self.cache.name + ".lock")
        lock.write_text("someone-else\n")
        self.assert_silent(self.run_helper("segment", LLM_ROOT=str(root)))
        self.assertEqual(self.calls_after(calls), 0)
        # Past its age the lock is taken to be abandoned and a refresh runs.
        old = time.time() - 3600
        os.utime(lock, (old, old))
        self.assert_silent(self.run_helper("segment", LLM_ROOT=str(root)))
        self.assertEqual(self.calls_after(calls), 1)

    @needs_status_script
    def test_a_successful_refresh_releases_its_lock(self):
        self.stale_cache()
        self.write_events([event("WI-1", "needs_decision")])
        self.run_helper("segment")
        lock = self.cache.with_name(self.cache.name + ".lock")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and lock.exists():
            time.sleep(0.2)
        self.assertFalse(lock.exists())
        self.assertEqual(json.loads(self.cache.read_text(encoding="utf-8"))["counts"], {"runs": 0, "reviews": 0, "decisions": 1})

    def test_a_slow_snapshot_is_cut_off_silently(self):
        root = self.fake_root("import time\ntime.sleep(30)\n")
        started = time.monotonic()
        result = self.run_helper("line", LLM_ROOT=str(root), PROGRAM_SUMMARY_TIMEOUT="0.5")
        self.assertLess(time.monotonic() - started, 10)
        self.assert_silent(result)

    def test_a_failing_snapshot_prints_nothing(self):
        bodies = (
            "import sys\nsys.stderr.write('boom')\nsys.exit(3)\n",
            "print('not json')\n",
            "print('[1, 2]')\n",
        )
        for body in bodies:
            with self.subTest(body=body):
                self.assert_silent(self.run_helper("line", LLM_ROOT=str(self.fake_root(body))))

    def test_a_missing_root_prints_nothing(self):
        self.assert_silent(self.run_helper("line", LLM_ROOT=str(self.dir / "nowhere")))

    def test_the_source_root_is_agentrc_source_then_llm_root_and_never_the_file_location(self):
        snapshot = {"events": {"items": [{"item": "WI-1", "event": "needs_decision"}]}}
        body = f"import json\nprint(json.dumps({snapshot!r}))\n"
        tree = self.fake_root(body)
        other = self.dir / "other-root"
        (other / "scripts").mkdir(parents=True)
        decoy = {"events": {"items": [{"item": "WI-8", "event": "needs_decision"}, {"item": "WI-9", "event": "needs_decision"}]}}
        (other / "scripts" / "program-status.py").write_text(f"import json\nprint(json.dumps({decoy!r}))\n", encoding="utf-8")
        one = "Program: 0 runs in flight, 0 pull requests awaiting your review, 1 item waiting on your decision.\n"
        two = "Program: 0 runs in flight, 0 pull requests awaiting your review, 2 items waiting on your decision.\n"

        def run(copy: Path, **extra: str) -> subprocess.CompletedProcess:
            env = self.env()
            del env["LLM_ROOT"]
            env.update(extra)
            return subprocess.run([sys.executable, str(copy), "line"], capture_output=True, text=True, env=env, timeout=60)

        # A copy that sits beside a scripts/ tree does not derive it as the root.
        copy = tree / "hooks" / "lib" / HELPER.name
        copy.parent.mkdir(parents=True)
        copy.write_bytes(HELPER.read_bytes())
        self.assert_silent(run(copy))
        result = run(copy, AGENTRC_SOURCE=str(tree))
        self.assertEqual((result.returncode, result.stdout), (0, one), result.stderr)
        result = run(copy, LLM_ROOT=str(tree))
        self.assertEqual((result.returncode, result.stdout), (0, one), result.stderr)
        result = run(copy, AGENTRC_SOURCE=str(other), LLM_ROOT=str(tree))
        self.assertEqual((result.returncode, result.stdout), (0, two), result.stderr)

    def test_an_unknown_command_prints_nothing(self):
        self.assert_silent(self.run_helper("bogus"))
        self.assert_silent(self.run_helper())


if __name__ == "__main__":
    unittest.main()
