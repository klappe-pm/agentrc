"""Behavioral tests for hooks/lib/subagent-cap.py, the per-parent live-subagent cap state.

Each test runs the module as subagent-cap-guard.sh does, a hook payload on
stdin, against a temporary RUNTIME_HOOK_STATE_DIR and GUARD_LOG_DIR, and reads
the lock files it leaves behind. Exit 0 allows, 3 denies, anything else is an
internal error the caller treats as allow (issue #275).
"""

from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

MODULE = Path(__file__).resolve().parents[3] / "stratarc" / "data" / "hooks" / "lib" / "subagent-cap.py"
PARENT = "parent-session"


class SubagentCapTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.state = self.root / "state"
        self.guard_log = self.root / "telemetry"
        self.guard_log.mkdir()

    def tearDown(self):
        self.directory.cleanup()

    def run_cap(self, payload, runtime="claude", cap="2", **extra):
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.root),
            "RUNTIME_HOOK_STATE_DIR": str(self.state),
            "GUARD_LOG_DIR": str(self.guard_log),
            "SUBAGENT_CAP_RUNTIME": runtime,
            "SUBAGENT_CAP_PER_PARENT": cap,
        }
        env.update(extra)
        text = payload if isinstance(payload, str) else json.dumps(payload)
        return subprocess.run(
            [sys.executable, str(MODULE)], input=text, env=env, capture_output=True, text=True, timeout=30
        )

    def pre(self, use_id, subagent_type="general-purpose", **kwargs):
        return self.run_cap(
            {
                "hook_event_name": "PreToolUse",
                "session_id": PARENT,
                "tool_use_id": use_id,
                "tool_input": {"subagent_type": subagent_type},
            },
            **kwargs,
        )

    def event(self, name, **fields):
        return dict({"hook_event_name": name, "session_id": PARENT}, **fields)

    @property
    def parent_dir(self):
        return self.state / PARENT

    def lock_names(self):
        if not self.parent_dir.is_dir():
            return []
        return sorted(p.name for p in self.parent_dir.iterdir() if p.name.endswith(".lock") and p.name != ".mutex.lock")

    def lock_fields(self, name):
        fields = {}
        for line in (self.parent_dir / name).read_text().splitlines():
            key, _, value = line.partition("=")
            fields[key] = value
        return fields

    def plant(self, name, **fields):
        self.parent_dir.mkdir(parents=True, exist_ok=True)
        (self.parent_dir / name).write_text("".join(f"{k}={v}\n" for k, v in fields.items()))

    def test_an_admitted_call_reserves_a_pending_lock_at_once(self):
        result = self.pre("call-1", subagent_type="Explore")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.lock_names(), ["pending-call-1.lock"])
        fields = self.lock_fields("pending-call-1.lock")
        self.assertEqual(fields["tool_use_id"], "call-1")
        self.assertEqual(fields["subagent_type"], "Explore")
        self.assertLessEqual(abs(int(fields["since"]) - int(time.time())), 5)

    def test_a_call_with_no_subagent_type_is_recorded_as_general_purpose(self):
        result = self.run_cap({"hook_event_name": "PreToolUse", "session_id": PARENT, "tool_use_id": "c"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.lock_fields("pending-c.lock")["subagent_type"], "general-purpose")

    def test_a_call_with_no_tool_use_id_still_reserves_a_lock(self):
        result = self.run_cap({"hook_event_name": "PreToolUse", "session_id": PARENT})
        self.assertEqual(result.returncode, 0, result.stderr)
        names = self.lock_names()
        self.assertEqual(len(names), 1)
        self.assertTrue(names[0].startswith("pending-"))

    def test_the_call_that_would_pass_the_cap_is_denied_and_reserves_nothing(self):
        self.assertEqual(self.pre("call-1").returncode, 0)
        self.assertEqual(self.pre("call-2").returncode, 0)
        denied = self.pre("call-3")
        self.assertEqual(denied.returncode, 3, denied.stderr)
        self.assertIn("already has 2 live subagent(s) in flight, at the cap of 2", denied.stdout)
        self.assertEqual(self.lock_names(), ["pending-call-1.lock", "pending-call-2.lock"])

    def test_confirmed_children_count_toward_the_cap(self):
        self.pre("call-1")
        self.run_cap(self.event("SubagentStart", agent_id="agent-1"))
        self.pre("call-2")
        self.assertEqual(self.pre("call-3").returncode, 3)

    def test_parents_are_counted_separately(self):
        self.pre("call-1", cap="1")
        other = self.run_cap(
            {"hook_event_name": "PreToolUse", "session_id": "other-parent", "tool_use_id": "call-2"}, cap="1"
        )
        self.assertEqual(other.returncode, 0, other.stderr)

    def test_the_parent_key_is_made_safe_for_a_directory_name(self):
        result = self.run_cap({"hook_event_name": "PreToolUse", "session_id": "../escape/x", "tool_use_id": "c"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.state / ".._escape_x" / "pending-c.lock").is_file())
        self.assertFalse((self.root / "escape").exists())

    def test_subagent_start_confirms_the_pending_lock_whose_type_matches(self):
        self.pre("call-1", subagent_type="general-purpose")
        self.pre("call-2", subagent_type="Explore")
        result = self.run_cap(self.event("SubagentStart", agent_id="agent-9", agent_type="Explore"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.lock_names(), ["child-agent-9.lock", "pending-call-1.lock"])
        fields = self.lock_fields("child-agent-9.lock")
        self.assertEqual((fields["tool_use_id"], fields["agent_id"]), ("call-2", "agent-9"))

    def test_subagent_start_falls_back_to_the_oldest_pending_lock(self):
        now = int(time.time())
        self.plant("pending-old.lock", since=now - 10, tool_use_id="old", subagent_type="a")
        self.plant("pending-new.lock", since=now - 5, tool_use_id="new", subagent_type="b")
        self.run_cap(self.event("SubagentStart", agent_id="agent-1", agent_type="unmatched"))
        self.assertEqual(self.lock_names(), ["child-agent-1.lock", "pending-new.lock"])
        self.assertEqual(self.lock_fields("child-agent-1.lock")["tool_use_id"], "old")

    def test_subagent_start_without_an_agent_id_confirms_nothing(self):
        self.pre("call-1")
        self.run_cap(self.event("SubagentStart"))
        self.assertEqual(self.lock_names(), ["pending-call-1.lock"])

    def test_subagent_stop_releases_only_the_named_child(self):
        self.pre("call-1")
        self.run_cap(self.event("SubagentStart", agent_id="agent-1"))
        self.pre("call-2")
        self.run_cap(self.event("SubagentStart", agent_id="agent-2"))
        self.run_cap(self.event("SubagentStop", agent_id="agent-1"))
        self.assertEqual(self.lock_names(), ["child-agent-2.lock"])
        self.run_cap(self.event("SubagentStop", agent_id="unknown"))
        self.assertEqual(self.lock_names(), ["child-agent-2.lock"])

    def test_post_tool_use_releases_its_pending_lock(self):
        self.pre("call-1")
        self.pre("call-2")
        self.run_cap(self.event("PostToolUse", tool_use_id="call-1"))
        self.assertEqual(self.lock_names(), ["pending-call-2.lock"])

    def test_post_tool_use_failure_releases_its_pending_lock(self):
        self.pre("call-1")
        self.run_cap(self.event("PostToolUseFailure", tool_use_id="call-1"))
        self.assertEqual(self.lock_names(), [])

    def test_with_a_start_signal_a_confirmed_child_outlives_its_tool_call(self):
        # A background subagent returns its tool call at once and keeps running.
        self.pre("call-1")
        self.run_cap(self.event("SubagentStart", agent_id="agent-1"))
        self.pre("call-2")
        self.run_cap(self.event("PostToolUse", tool_use_id="call-1"))
        self.assertEqual(self.lock_names(), ["child-agent-1.lock", "pending-call-2.lock"])

    def test_with_a_start_signal_an_unnamed_post_releases_the_oldest_pending_lock(self):
        now = int(time.time())
        self.plant("child-a.lock", since=now - 20, tool_use_id="x", agent_id="a")
        self.plant("pending-old.lock", since=now - 10, tool_use_id="old")
        self.plant("pending-new.lock", since=now - 5, tool_use_id="new")
        self.run_cap(self.event("PostToolUse"))
        self.assertEqual(self.lock_names(), ["child-a.lock", "pending-new.lock"])

    def test_without_a_start_signal_post_releases_the_confirmed_lock_it_names(self):
        now = int(time.time())
        self.plant("child-a.lock", since=now - 5, tool_use_id="call-1", agent_id="a")
        self.plant("pending-b.lock", since=now - 3, tool_use_id="call-2")
        self.run_cap(self.event("PostToolUse", tool_use_id="call-1"), runtime="codex")
        self.assertEqual(self.lock_names(), ["pending-b.lock"])

    def test_without_a_start_signal_post_falls_back_to_the_oldest_lock(self):
        now = int(time.time())
        self.plant("child-a.lock", since=now - 20, tool_use_id="x", agent_id="a")
        self.plant("pending-b.lock", since=now - 10, tool_use_id="y")
        self.run_cap(self.event("PostToolUse", tool_use_id="unknown"), runtime="codex")
        self.assertEqual(self.lock_names(), ["pending-b.lock"])

    def test_a_lock_past_the_ttl_is_pruned(self):
        now = int(time.time())
        self.plant("child-stale.lock", since=now - 100, tool_use_id="s", agent_id="stale")
        self.plant("child-live.lock", since=now - 5, tool_use_id="l", agent_id="live")
        result = self.pre("call-1", cap="2", SUBAGENT_CAP_TTL_SECS="50")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.lock_names(), ["child-live.lock", "pending-call-1.lock"])

    def test_a_lock_with_no_since_is_pruned(self):
        self.plant("child-broken.lock", tool_use_id="b")
        self.pre("call-1", cap="1")
        self.assertEqual(self.lock_names(), ["pending-call-1.lock"])

    def test_an_unconfirmed_lock_past_the_window_is_pruned_only_with_a_start_signal(self):
        now = int(time.time())
        self.plant("pending-waiting.lock", since=now - 100, tool_use_id="w")
        self.run_cap(self.event("SubagentStop", agent_id="none"), runtime="codex")
        self.assertEqual(self.lock_names(), ["pending-waiting.lock"])
        self.run_cap(self.event("SubagentStop", agent_id="none"), runtime="opencode")
        self.assertEqual(self.lock_names(), [])

    def test_a_confirmed_lock_is_not_pruned_by_the_confirm_window(self):
        now = int(time.time())
        self.plant("child-a.lock", since=now - 100, tool_use_id="x", agent_id="a")
        self.run_cap(self.event("SubagentStop", agent_id="none"))
        self.assertEqual(self.lock_names(), ["child-a.lock"])

    def test_a_pending_lock_a_guard_denied_is_released(self):
        self.pre("call-denied")
        self.pre("call-kept")
        day = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
        records = [
            {"decision": "deny", "tool_use_id": "call-denied"},
            {"decision": "allow", "tool_use_id": "call-kept"},
            "not json",
        ]
        (self.guard_log / f"guard-events-{day}.jsonl").write_text(
            "\n".join(r if isinstance(r, str) else json.dumps(r) for r in records) + "\n"
        )
        result = self.pre("call-3")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.lock_names(), ["pending-call-3.lock", "pending-call-kept.lock"])

    def test_a_deny_record_from_yesterday_still_releases(self):
        self.pre("call-denied")
        day = (datetime.datetime.now(datetime.timezone.utc).date() - datetime.timedelta(days=1)).isoformat()
        (self.guard_log / f"guard-events-{day}.jsonl").write_text(
            json.dumps({"decision": "deny", "tool_use_id": "call-denied"}) + "\n"
        )
        self.run_cap(self.event("SubagentStop", agent_id="none"))
        self.assertEqual(self.lock_names(), [])

    def test_an_unknown_event_changes_nothing(self):
        self.pre("call-1", cap="1")
        result = self.run_cap(self.event("Notification"), cap="1")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.lock_names(), ["pending-call-1.lock"])

    def test_bad_input_is_an_internal_error_never_a_deny(self):
        cases = {
            "not an object": ("[]", {}),
            "no parent": (json.dumps({"hook_event_name": "PreToolUse"}), {}),
            "cap of zero": (json.dumps(self.event("PreToolUse")), {"cap": "0"}),
            "non-numeric cap": (json.dumps(self.event("PreToolUse")), {"cap": "many"}),
            "non-numeric ttl": (json.dumps(self.event("PreToolUse")), {"SUBAGENT_CAP_TTL_SECS": "x"}),
            "invalid json": ("{", {}),
        }
        for label, (payload, extra) in cases.items():
            with self.subTest(label):
                result = self.run_cap(payload, **extra)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn("subagent-cap:", result.stderr)

    def test_the_parent_falls_back_to_subagent_cap_ppid(self):
        result = self.run_cap(
            {"hook_event_name": "PreToolUse", "tool_use_id": "c"}, SUBAGENT_CAP_PPID="from-env"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.state / "from-env" / "pending-c.lock").is_file())


if __name__ == "__main__":
    unittest.main()
