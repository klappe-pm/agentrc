"""Tests for the service and port check and the service installer in stratarc.components.

A declared service names its launchd label, its port and its plist; a known competitor is declared wanted: false with the match that finds it and the line that removes it. The check compares the process listening on each declared port with the owner's pid from launchctl print and names any other holder, with its removal line when it is a declared competitor. The installer copies the declared plist into a LaunchAgents directory and bootstraps it; it never stops a competitor.

Every probe here is a fake: a runner that answers argv from a table, or stub launchctl, lsof and ps executables, and a fixture LaunchAgents directory. No test reads this machine's launchd state or ports, and every test runs on a fixture manifest and fixture plists in a temporary directory.
"""

from __future__ import annotations

import io
import json
import os
import plistlib
import stat
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

from stratarc import components

UID = 501
OWNER_LABEL = "com.example.embeddings"
APP_EXE = "/Applications/Vendor.app/Contents/Resources/embedder"
APP_REMOVE = "killall Vendor, then remove Vendor from System Settings > General > Login Items"
BREW_REMOVE = "brew services stop embedder"
# Homebrew's agent runs the binary from either of two paths, so the declared
# competitor matches both.
BREW_MATCHES = ["/opt/homebrew/Cellar/embedder/", "/opt/homebrew/opt/embedder/bin/embedder"]
# A value launchctl print shows in a job's environment. It must never reach
# any output of the check.
ENV_VALUE = "fixture-environment-value-never-printed"


@pytest.fixture
def run_env(repo_root, stratarc_home):
    """The environment for a subprocess that imports the checkout's stratarc."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(repo_root)
    return env


def entry(name, **extra):
    value = {"name": name, "runtimes": ["claude"], "owner": "example", "wanted": True}
    value.update(extra)
    return value


def manifest(*services):
    return {"version": 1, "services": list(services)}


EMBEDDINGS = entry(
    "embeddings",
    kind="launchd",
    label=OWNER_LABEL,
    port=11434,
    plist="scripts/launchd/com.example.embeddings.plist",
    installed=True,
)
VENDOR_APP = entry(
    "vendor-app",
    owner="Vendor.app",
    wanted=False,
    kind="app",
    label="Vendor",
    port=11434,
    match="/Applications/Vendor.app/",
    remove=APP_REMOVE,
)
BREW_EMBEDDER = entry(
    "brew-embedder",
    owner="Homebrew",
    wanted=False,
    kind="launchd",
    label="sh.brew.embedder",
    port=11434,
    match=BREW_MATCHES,
    remove=BREW_REMOVE,
)
COMPETING_MANIFEST = manifest(EMBEDDINGS, VENDOR_APP, BREW_EMBEDDER)


def launchctl_print(label, pid=None, state="running"):
    """The shape of launchctl print gui/<uid>/<label>: top-level keys one tab
    in, nested blocks deeper, and an environment block holding a value."""
    lines = [
        f"gui/{UID}/{label} = {{",
        "\tactive count = 1",
        f"\tpath = /home/fixture/Library/LaunchAgents/{label}.plist",
        "\ttype = LaunchAgent",
        f"\tstate = {state}",
        "",
        "\tprogram = /opt/homebrew/bin/embedder",
        "\tenvironment = {",
        f"\t\tFIXTURE_KEY => {ENV_VALUE}",
        "\t}",
        "",
        "\tdomain = gui/501 [100005]",
    ]
    if pid is not None:
        lines.append(f"\tpid = {pid}")
    lines += ["\tendpoints = {", "\t\tpid = 1", "\t}", "}"]
    return "\n".join(lines) + "\n"


class FakeRunner:
    """Answers argv from a table; an lsof call not in it finds no listener
    (exit 1, as the real one does), and anything else not in it fails as
    launchctl does for an unloaded label (exit 113). Records every call.

    A table value may be a list: each call to that same argv consumes the
    next entry, and the last entry repeats once the list is exhausted. This
    lets a test give the same launchctl print call a different answer before
    and after a bootstrap, to exercise install_service's rollback paths."""

    def __init__(self, table):
        self.table = {tuple(k): v for k, v in table.items()}
        self.calls = []
        self._sequence_positions = {}

    def __call__(self, argv):
        self.calls.append(list(argv))
        key = tuple(argv)
        answer = self.table.get(key)
        if isinstance(answer, list):
            position = self._sequence_positions.get(key, 0)
            self._sequence_positions[key] = position + 1
            answer = answer[min(position, len(answer) - 1)]
        if answer is None and argv and argv[0] == "lsof":
            return (1, "", "")
        if answer is None:
            return (113, "", "Could not find service in domain for port\n")
        if isinstance(answer, str):
            return (0, answer, "")
        return answer


def probe(table, uid=UID):
    runner = FakeRunner(table)
    return components.Probe(runner=runner, uid=uid), runner


def print_argv(label):
    return ("launchctl", "print", f"gui/{UID}/{label}")


def lsof_argv(port):
    return ("lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fp")


def comm_argv(pid):
    return ("ps", "-o", "comm=", "-p", str(pid))


PS_ALL = ("ps", "-axo", "pid=,comm=")


def failing(findings):
    return [f for f in findings if f.failing]


def text(findings):
    return "\n".join(f.render() for f in findings)


class TestServiceCheck:
    def test_unlisted_service_port_has_no_listener_in_the_fixture(self):
        p, _ = probe({})
        assert p.listeners(7373) == []

    def test_the_app_conflict_is_reported_with_its_removal_line(self):
        # Vendor.app holds 11434 while the owned service is loaded but not
        # the holder: the conflict names the holder and prints its removal.
        p, _ = probe(
            {
                print_argv(OWNER_LABEL): launchctl_print(OWNER_LABEL, pid=7966),
                lsof_argv(11434): "p812\nf3\n",
                comm_argv(812): APP_EXE + "\n",
                PS_ALL: f"  1 /sbin/launchd\n 812 {APP_EXE}\n7966 /opt/homebrew/Cellar/embedder/0.12.0/bin/embedder\n",
            }
        )
        findings = components.check_services(COMPETING_MANIFEST, p)
        conflicts = [f for f in findings if f.kind == "port conflict"]
        assert len(conflicts) == 1, text(findings)
        conflict = conflicts[0]
        assert conflict.failing
        assert conflict.service == "embeddings"
        assert "port 11434" in conflict.message
        assert APP_EXE in conflict.message
        assert "pid 812" in conflict.message
        assert "vendor-app" in conflict.message
        assert APP_REMOVE in conflict.message
        # The competitor named in the conflict is not reported a second time.
        assert [f.kind for f in findings if f.service == "vendor-app"] == []

    def test_the_conflict_is_reported_when_the_owner_is_respawning(self):
        # The owned job exits on bind and is spawn scheduled with no pid, while
        # the app holds the port.
        p, _ = probe(
            {
                print_argv(OWNER_LABEL): launchctl_print(OWNER_LABEL, pid=None, state="spawn scheduled"),
                lsof_argv(11434): "p2217\nf3\n",
                comm_argv(2217): APP_EXE + "\n",
                PS_ALL: f"2217 {APP_EXE}\n",
            }
        )
        findings = components.check_services(COMPETING_MANIFEST, p)
        kinds = [f.kind for f in failing(findings)]
        assert "port conflict" in kinds, text(findings)
        assert "not running" in kinds
        assert APP_REMOVE in text(findings)

    def test_a_loaded_unwanted_launchd_job_holding_the_port_is_named(self):
        p, _ = probe(
            {
                print_argv(OWNER_LABEL): launchctl_print(OWNER_LABEL, pid=7966),
                print_argv("sh.brew.embedder"): launchctl_print("sh.brew.embedder", pid=900),
                lsof_argv(11434): "p900\nf3\n",
                comm_argv(900): "/opt/homebrew/Cellar/embedder/0.12.0/bin/embedder\n",
                PS_ALL: "900 /opt/homebrew/Cellar/embedder/0.12.0/bin/embedder\n",
            }
        )
        findings = components.check_services(COMPETING_MANIFEST, p)
        conflict = [f for f in findings if f.kind == "port conflict"]
        assert len(conflict) == 1, text(findings)
        assert "brew-embedder" in conflict[0].message
        assert BREW_REMOVE in conflict[0].message

    def test_the_owner_holding_its_port_is_not_a_failure(self):
        p, _ = probe(
            {
                print_argv(OWNER_LABEL): launchctl_print(OWNER_LABEL, pid=7966),
                lsof_argv(11434): "p7966\nf3\n",
                PS_ALL: "7966 /opt/homebrew/Cellar/embedder/0.12.0/bin/embedder\n",
            }
        )
        findings = components.check_services(COMPETING_MANIFEST, p)
        assert failing(findings) == [], text(findings)
        assert "pid 7966" in text(findings)
        assert "holds port 11434" in text(findings)

    def test_an_unwanted_app_running_off_the_port_is_returned(self):
        p, _ = probe(
            {
                print_argv(OWNER_LABEL): launchctl_print(OWNER_LABEL, pid=7966),
                lsof_argv(11434): "p7966\nf3\n",
                PS_ALL: f"7966 /opt/homebrew/bin/embedder\n 812 /Applications/Vendor.app/Contents/MacOS/Vendor\n",
            }
        )
        findings = components.check_services(COMPETING_MANIFEST, p)
        returned = [f for f in findings if f.kind == "returned"]
        assert [f.service for f in returned] == ["vendor-app"], text(findings)
        assert returned[0].failing
        assert APP_REMOVE in returned[0].message

    def test_an_unwanted_launchd_job_loaded_off_the_port_is_returned(self):
        p, _ = probe(
            {
                print_argv(OWNER_LABEL): launchctl_print(OWNER_LABEL, pid=7966),
                print_argv("sh.brew.embedder"): launchctl_print("sh.brew.embedder", pid=None, state="not running"),
                lsof_argv(11434): "p7966\nf3\n",
                PS_ALL: "7966 /opt/homebrew/bin/embedder\n",
            }
        )
        findings = components.check_services(COMPETING_MANIFEST, p)
        returned = [f for f in findings if f.kind == "returned"]
        assert [f.service for f in returned] == ["brew-embedder"], text(findings)
        assert BREW_REMOVE in returned[0].message

    def test_recorded_homebrew_label_is_detected_off_port_with_a_cellar_executable(self):
        p, _ = probe(
            {
                print_argv(OWNER_LABEL): launchctl_print(OWNER_LABEL, pid=7966),
                print_argv("sh.brew.embedder"): launchctl_print("sh.brew.embedder", pid=900),
                lsof_argv(11434): "p7966\n",
                PS_ALL: "900 /opt/homebrew/Cellar/embedder/0.12.0/bin/embedder\n",
            }
        )
        findings = components.check_services(COMPETING_MANIFEST, p)
        returned = [f for f in findings if f.service == "brew-embedder" and f.kind == "returned"]
        assert len(returned) == 1, text(findings)
        assert returned[0].failing

    @pytest.mark.parametrize(
        "executable",
        ["/opt/homebrew/opt/embedder/bin/embedder", "/opt/homebrew/Cellar/embedder/0.12.0/bin/embedder"],
    )
    def test_unloaded_homebrew_job_holding_port_is_named_by_either_executable_path(self, executable):
        p, _ = probe(
            {
                print_argv(OWNER_LABEL): launchctl_print(OWNER_LABEL, pid=7966),
                lsof_argv(11434): "p900\nf3\n",
                comm_argv(900): executable + "\n",
                PS_ALL: f"900 {executable}\n",
            }
        )
        findings = components.check_services(COMPETING_MANIFEST, p)
        conflicts = [f for f in findings if f.kind == "port conflict"]
        assert len(conflicts) == 1, text(findings)
        assert "brew-embedder" in conflicts[0].message
        assert BREW_REMOVE in conflicts[0].message

    def test_a_declared_service_that_is_not_loaded_fails_and_names_the_installer(self):
        p, _ = probe({PS_ALL: "", lsof_argv(11434): (1, "", "")})
        findings = components.check_services(manifest(EMBEDDINGS), p)
        not_loaded = [f for f in findings if f.kind == "not loaded"]
        assert len(not_loaded) == 1, text(findings)
        assert not_loaded[0].failing
        assert "--install-service embeddings" in not_loaded[0].message

    def test_a_service_declared_not_installed_is_reported_and_does_not_fail(self, tmp_path):
        deferred = dict(EMBEDDINGS, installed=False, port=None)
        del deferred["port"]
        p, _ = probe({PS_ALL: ""})
        findings = components.check_services(manifest(deferred), p, launch_agents=tmp_path / "empty-agents")
        assert failing(findings) == [], text(findings)
        assert [f.kind for f in findings] == ["not installed"]
        assert "--install-service embeddings" in findings[0].message

    @pytest.mark.parametrize("name", ["weekly-audit", "usage-collect"])
    def test_an_idle_calendar_job_is_loaded_without_a_pid(self, name):
            label = f"com.example.{name}"
            scheduled = entry(name, kind="launchd", label=label, installed=False)
            p, _ = probe({print_argv(label): launchctl_print(label, pid=None, state="waiting")})
            findings = components.check_services(manifest(scheduled), p)
            assert failing(findings) == [], text(findings)
            assert [f.kind for f in findings] == ["loaded"], text(findings)

    def test_a_running_owner_with_nothing_on_its_port_is_not_listening(self):
        p, _ = probe(
            {
                print_argv(OWNER_LABEL): launchctl_print(OWNER_LABEL, pid=7966),
                lsof_argv(11434): (1, "", ""),
                PS_ALL: "",
            }
        )
        findings = components.check_services(manifest(EMBEDDINGS), p)
        assert [f.kind for f in failing(findings)] == ["not listening"], text(findings)

    def test_a_missing_launchctl_is_unreadable_never_empty(self):
        p, _ = probe({print_argv(OWNER_LABEL): (127, "", "")})
        findings = components.check_services(manifest(EMBEDDINGS), p)
        assert [f.kind for f in findings] == ["unreadable"], text(findings)
        assert findings[0].failing
        assert "launchctl" in findings[0].message

    def test_an_lsof_error_is_unreadable_not_an_empty_port(self):
        # lsof exits 1 when nothing listens; any other failure is not "free".
        p, _ = probe(
            {
                print_argv(OWNER_LABEL): launchctl_print(OWNER_LABEL, pid=7966),
                lsof_argv(11434): (2, "", "lsof: WARNING"),
            }
        )
        findings = components.check_services(manifest(EMBEDDINGS), p)
        assert [f.kind for f in findings] == ["unreadable"], text(findings)
        assert "lsof" in findings[0].message

    def test_a_launchctl_print_error_other_than_not_found_is_unreadable(self):
        p, _ = probe({print_argv(OWNER_LABEL): (5, "", "Input/output error")})
        findings = components.check_services(manifest(EMBEDDINGS), p)
        assert [f.kind for f in findings] == ["unreadable"], text(findings)

    def test_a_ps_error_is_unreadable_not_an_empty_process_list(self):
        p, _ = probe(
            {
                print_argv(OWNER_LABEL): launchctl_print(OWNER_LABEL, pid=7966),
                lsof_argv(11434): "p7966\n",
                PS_ALL: (1, "", "ps: error"),
            }
        )
        findings = components.check_services(manifest(EMBEDDINGS, VENDOR_APP), p)
        assert [f.kind for f in findings] == ["unreadable"], text(findings)

    def test_a_probe_that_times_out_is_unreadable_not_a_free_port(self):
        # lsof exits 1 for "nothing listens", so a timeout must not look like it.
        timeout = subprocess.TimeoutExpired("lsof", components.PROBE_TIMEOUT)
        with mock.patch.object(components.subprocess, "run", side_effect=timeout):
            with pytest.raises(components.ProbeUnavailable):
                components.Probe(uid=UID).listeners(11434)
            with pytest.raises(components.ProbeUnavailable):
                components.Probe(uid=UID).job(OWNER_LABEL)

    def test_no_service_declared_is_one_line_and_no_probe(self):
        p, runner = probe({})
        findings = components.check_services(manifest(), p)
        assert findings == []
        assert runner.calls == []

    def test_no_value_from_launchctl_print_reaches_any_output(self):
        p, _ = probe(
            {
                print_argv(OWNER_LABEL): launchctl_print(OWNER_LABEL, pid=7966),
                print_argv("sh.brew.embedder"): launchctl_print("sh.brew.embedder", pid=900),
                lsof_argv(11434): "p812\nf3\n",
                comm_argv(812): APP_EXE + "\n",
                PS_ALL: f"812 {APP_EXE}\n",
            }
        )
        findings = components.check_services(COMPETING_MANIFEST, p)
        assert ENV_VALUE not in text(findings)
        assert ENV_VALUE not in "\n".join(components.service_report_lines(findings))

    def test_only_the_top_level_pid_is_the_owner_pid(self):
        # The endpoints block carries a nested "pid = 1"; the job is not running.
        p, _ = probe({print_argv(OWNER_LABEL): launchctl_print(OWNER_LABEL, pid=None, state="not running")})
        job = p.job(OWNER_LABEL)
        assert job is not None
        assert job.pid is None
        assert job.state == "not running"


class TestServiceReportLines:
    def test_a_clean_check_is_one_line(self):
        assert components.service_report_lines([components.ServiceFinding("x", "loaded", "loaded as l (pid 1)", False)]) == ["Services reported: 1. Findings that need action: 0.", "","| service | state | detail |", "|---|---|---|", "| x | loaded | loaded as l (pid 1) |"]

    def test_a_pipe_in_a_detail_is_escaped(self):
        lines = components.service_report_lines([components.ServiceFinding("x", "returned", "a | b", True)])
        assert "| x | returned | a &#124; b |" in lines
        assert lines[0] == "Services reported: 1. Findings that need action: 1."

    def test_nothing_declared_says_so(self):
        assert components.service_report_lines([]) == ["No services are declared in components.json."]


def write_stub(directory: Path, name: str, body: str) -> Path:
    path = directory / name
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def stub_launchctl(directory: Path, log: Path, state: Path, *, bootstrap_rc: int = 0) -> Path:
    """A launchctl that logs argv, answers print from a loaded marker, and
    marks the label loaded on bootstrap and unloaded on bootout."""
    return write_stub(
        directory,
        "launchctl",
        f"""echo "$*" >> "{log}"
case "$1" in
  print)
    label="${{2##*/}}"
    if [ -f "{state}/$label" ]; then printf 'x = {{\\n\\tstate = running\\n\\tpid = 4242\\n}}\\n'; exit 0; fi
    exit 113 ;;
  bootstrap)
    [ {bootstrap_rc} = 0 ] || {{ echo "Bootstrap failed: 5: Input/output error" >&2; exit {bootstrap_rc}; }}
    label="$(basename "$3" .plist)"; touch "{state}/$label"; exit 0 ;;
  bootout)
    label="${{2##*/}}"; rm -f "{state}/$label"; exit 0 ;;
esac
exit 64
""",
    )


def owned_plist(root: Path, label: str) -> str:
    relative = f"scripts/launchd/{label}.plist"
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        plistlib.dump({"Label": label, "ProgramArguments": ["/usr/bin/true"]}, handle)
    return relative


class TestInstallService:
    @pytest.fixture(autouse=True)
    def workspace(self, tmp_path):
        base = tmp_path
        self.root = base / "repo"
        self.agents = base / "LaunchAgents"
        self.bin = base / "bin"
        self.bin.mkdir()
        self.state = base / "state"
        self.state.mkdir()
        self.log = base / "launchctl.log"
        self.log.write_text("")
        self.label = "com.example.weekly-audit"
        self.relative = owned_plist(self.root, self.label)
        self.entry = entry("weekly-audit", kind="launchd", label=self.label, plist=self.relative, installed=False)

    def install(self, document, name, **stub):
        launchctl = stub_launchctl(self.bin, self.log, self.state, **stub)
        p = components.Probe(launchctl=str(launchctl), uid=UID)
        return components.install_service(document, name, root=self.root, launch_agents=self.agents, probe=p, platform="darwin")

    def calls(self):
        return [line for line in self.log.read_text().splitlines() if line]

    def test_copies_the_plist_and_bootstraps_it(self):
        code = self.install(manifest(self.entry), "weekly-audit")
        assert code == 0
        dest = self.agents / f"{self.label}.plist"
        assert dest.read_bytes() == (self.root / self.relative).read_bytes()
        assert f"bootstrap gui/{UID} {dest}" in self.calls()
        assert f"bootout gui/{UID}/{self.label}" not in self.calls()

    def test_a_missing_job_fails_after_its_plist_was_installed(self):
        document = manifest(self.entry)
        assert self.install(document, "weekly-audit") == 0
        (self.state / self.label).unlink()
        (self.agents / f"{self.label}.plist").unlink()
        launchctl = stub_launchctl(self.bin, self.log, self.state)
        p = components.Probe(launchctl=str(launchctl), uid=UID)
        findings = components.check_services(document, p, launch_agents=self.agents)
        assert [f.kind for f in failing(findings)] == ["not loaded"], text(findings)

    def test_a_loaded_service_is_booted_out_before_the_new_plist_is_bootstrapped(self):
        (self.state / self.label).touch()
        # A loaded job always got that way through install_service, which
        # always writes a plist to this exact destination first; this backup
        # is what a failed replacement would be restored from.
        self.agents.mkdir(parents=True, exist_ok=True)
        (self.agents / f"{self.label}.plist").write_bytes(b"previous plist bytes")
        code = self.install(manifest(self.entry), "weekly-audit")
        assert code == 0
        calls = self.calls()
        dest = self.agents / f"{self.label}.plist"
        assert calls.index(f"bootout gui/{UID}/{self.label}") < calls.index(f"bootstrap gui/{UID} {dest}")

    def test_a_loaded_service_with_no_backing_plist_is_refused(self):
        """A loaded job with nothing at its expected destination cannot be
        safely replaced: there is no previous plist to restore if the new
        one fails to bootstrap, so install_service refuses up front rather
        than overwrite blind. This is a behavior change: before this fix,
        such a replacement proceeded with no backup at all."""
        (self.state / self.label).touch()
        code = self.install(manifest(self.entry), "weekly-audit")
        assert code == 1
        assert "bootstrap" not in " ".join(self.calls())
        assert "bootout" not in " ".join(self.calls())

    def test_a_failed_bootstrap_restores_the_previous_plist_and_reloads_the_old_job(self):
        """Replacing
        a loaded service must not leave it stopped with no way back if the
        new plist cannot bootstrap."""
        previous_bytes = b"the previous plist, byte for byte"
        self.agents.mkdir(parents=True)
        destination = self.agents / f"{self.label}.plist"
        destination.write_bytes(previous_bytes)
        table = {
            print_argv(self.label): launchctl_print(self.label, pid=9999, state="running"),
            ("launchctl", "bootout", f"gui/{UID}/{self.label}"): (0, "", ""),
            ("launchctl", "bootstrap", f"gui/{UID}", str(destination)): [
                (5, "", "Bootstrap failed: 5: Input/output error"),
                (0, "", ""),
            ],
        }
        p, runner = probe(table)
        code = components.install_service(
            manifest(self.entry), "weekly-audit", root=self.root, launch_agents=self.agents, probe=p, platform="darwin"
        )
        assert code == 1
        assert destination.read_bytes() == previous_bytes
        bootstrap_calls = [c for c in runner.calls if c[1] == "bootstrap"]
        bootout_calls = [c for c in runner.calls if c[1] == "bootout"]
        assert len(bootstrap_calls) == 2
        assert len(bootout_calls) == 1

    def test_a_probe_failure_during_bootout_itself_still_restores_the_previous_plist(self):
        """A ProbeUnavailable raised by the bootout call itself (never
        reaching a successful bootstrap, so bootstrapped stays False) must
        still restore the plist the copy already overwrote; gating the
        restore on bootstrapped alone would skip it here."""
        previous_bytes = b"the previous plist, byte for byte"
        self.agents.mkdir(parents=True)
        destination = self.agents / f"{self.label}.plist"
        destination.write_bytes(previous_bytes)
        table = {
            print_argv(self.label): launchctl_print(self.label, pid=9999, state="running"),
            ("launchctl", "bootout", f"gui/{UID}/{self.label}"): (127, "", ""),
        }
        p, _ = probe(table)
        code = components.install_service(
            manifest(self.entry), "weekly-audit", root=self.root, launch_agents=self.agents, probe=p, platform="darwin"
        )
        assert code == 1
        assert destination.read_bytes() == previous_bytes

    def test_a_fresh_install_with_no_job_after_bootstrap_removes_the_unverified_plist(self):
        """The first-time-install case: bootstrap reports success but the job
        cannot be confirmed loaded. Nothing was previously running, so
        restoring means leaving no half-installed plist behind."""
        destination = self.agents / f"{self.label}.plist"
        table = {
            print_argv(self.label): [(113, "", ""), (113, "", "")],
            ("launchctl", "bootstrap", f"gui/{UID}", str(destination)): (0, "", ""),
        }
        p, runner = probe(table)
        code = components.install_service(
            manifest(self.entry), "weekly-audit", root=self.root, launch_agents=self.agents, probe=p, platform="darwin"
        )
        assert code == 1
        assert not destination.exists()
        assert [c for c in runner.calls if c[1] == "bootout"] == []

    def test_a_replacement_with_no_job_after_bootstrap_restores_the_previous_service(self):
        """The same post-bootstrap verification gap as above, but replacing
        an already-loaded service: the previous plist and job must come
        back, not just the plist file the first-time-install case covers."""
        previous_bytes = b"the previous plist, byte for byte"
        self.agents.mkdir(parents=True)
        destination = self.agents / f"{self.label}.plist"
        destination.write_bytes(previous_bytes)
        table = {
            print_argv(self.label): [
                launchctl_print(self.label, pid=9999, state="running"),
                (113, "", ""),
            ],
            ("launchctl", "bootout", f"gui/{UID}/{self.label}"): (0, "", ""),
            ("launchctl", "bootstrap", f"gui/{UID}", str(destination)): (0, "", ""),
        }
        p, runner = probe(table)
        code = components.install_service(
            manifest(self.entry), "weekly-audit", root=self.root, launch_agents=self.agents, probe=p, platform="darwin"
        )
        assert code == 1
        assert destination.read_bytes() == previous_bytes
        assert len([c for c in runner.calls if c[1] == "bootout"]) == 1
        assert len([c for c in runner.calls if c[1] == "bootstrap"]) == 2

    def test_an_unreadable_verification_after_a_fresh_bootstrap_stops_the_replacement(self):
        """A first-time install whose post-bootstrap verification raised
        ProbeUnavailable must not leave the unverified replacement in place."""
        destination = self.agents / f"{self.label}.plist"
        table = {
            print_argv(self.label): [(113, "", ""), (2, "", "boom")],
            ("launchctl", "bootstrap", f"gui/{UID}", str(destination)): (0, "", ""),
            ("launchctl", "bootout", f"gui/{UID}/{self.label}"): (0, "", ""),
        }
        p, runner = probe(table)
        code = components.install_service(
            manifest(self.entry), "weekly-audit", root=self.root, launch_agents=self.agents, probe=p, platform="darwin"
        )
        assert code == 1
        assert not destination.exists()
        assert len([c for c in runner.calls if c[1] == "bootout"]) == 1

    def test_install_service_fails_when_the_post_install_check_finds_a_failing_service(self):
        """A
        finding for a service that bootstrapped but is not actually running
        must not be reported as a successful install."""
        ported = dict(self.entry, port=9999)
        destination = self.agents / f"{self.label}.plist"
        table = {
            print_argv(self.label): [
                (113, "", ""),
                launchctl_print(self.label, pid=4242, state="running"),
                launchctl_print(self.label, pid=None, state="waiting"),
            ],
            ("launchctl", "bootstrap", f"gui/{UID}", str(destination)): (0, "", ""),
        }
        p, _ = probe(table)
        code = components.install_service(
            manifest(ported), "weekly-audit", root=self.root, launch_agents=self.agents, probe=p, platform="darwin"
        )
        assert code == 1

    def test_install_service_succeeds_when_the_post_install_check_is_clean(self):
        ported = dict(self.entry, port=9999)
        destination = self.agents / f"{self.label}.plist"
        table = {
            print_argv(self.label): [(113, "", ""), launchctl_print(self.label, pid=4242, state="running")],
            ("launchctl", "bootstrap", f"gui/{UID}", str(destination)): (0, "", ""),
            lsof_argv(9999): (0, "p4242\n", ""),
        }
        p, _ = probe(table)
        code = components.install_service(
            manifest(ported), "weekly-audit", root=self.root, launch_agents=self.agents, probe=p, platform="darwin"
        )
        assert code == 0

    def test_an_undeclared_or_unwanted_name_is_refused_and_nothing_is_copied(self):
        unwanted = dict(self.entry, wanted=False, match="x", remove="y")
        assert self.install(manifest(self.entry), "nothing-by-this-name") == 1
        assert self.install(manifest(unwanted), "weekly-audit") == 1
        assert not self.agents.exists()
        assert self.calls() == []

    def test_a_service_without_a_plist_is_refused(self):
        app = entry("some-app", kind="app", label="Some")
        assert self.install(manifest(app), "some-app") == 1
        assert self.calls() == []

    def test_a_plist_whose_label_differs_from_the_declared_label_is_refused(self):
        wrong = dict(self.entry, label="com.example.something-else")
        assert self.install(manifest(wrong), "weekly-audit") == 1
        assert not self.agents.exists()

    def test_a_launch_agents_path_that_cannot_hold_the_plist_fails_cleanly(self):
        self.agents.parent.mkdir(parents=True, exist_ok=True)
        self.agents.write_text("a file, not a directory")
        assert self.install(manifest(self.entry), "weekly-audit") == 1
        assert "bootstrap" not in " ".join(self.calls())

    def test_a_failed_bootstrap_fails_the_install(self):
        assert self.install(manifest(self.entry), "weekly-audit", bootstrap_rc=5) == 1


class TestCommandLine:
    """stratarc.components --services and --install-service against stubbed launchctl, lsof and ps and a fixture LaunchAgents directory."""

    @pytest.fixture(autouse=True)
    def workspace(self, tmp_path, run_env):
        self.base = tmp_path
        self.env = run_env
        self.bin = self.base / "bin"
        self.bin.mkdir()

    def run_components(self, document, *args):
        path = self.base / "components.json"
        path.write_text(json.dumps(document))
        return subprocess.run(
            [sys.executable, "-m", "stratarc.components", "--manifest", str(path), *args],
            capture_output=True,
            text=True,
            timeout=60,
            env=self.env,
        )

    def test_services_reports_the_conflict_with_its_removal_line_and_exits_1(self):
        launchctl = write_stub(
            self.bin,
            "launchctl",
            f"""case "$2" in
  */{OWNER_LABEL}) printf 'x = {{\\n\\tstate = running\\n\\tpid = 7966\\n\\tenvironment = {{\\n\\t\\tK => {ENV_VALUE}\\n\\t}}\\n}}\\n'; exit 0 ;;
esac
exit 113
""",
        )
        lsof = write_stub(self.bin, "lsof", 'printf "p812\\nf3\\n"\n')
        ps = write_stub(
            self.bin,
            "ps",
            f"""if [ "$1" = "-axo" ]; then printf ' 812 {APP_EXE}\\n'; else printf '{APP_EXE}\\n'; fi
""",
        )
        result = self.run_components(
            COMPETING_MANIFEST,
            "--services",
            "--launchctl",
            str(launchctl),
            "--lsof",
            str(lsof),
            "--ps",
            str(ps),
        )
        assert result.returncode == 1, result.stdout + result.stderr
        assert "port conflict" in result.stdout
        assert APP_REMOVE in result.stdout
        assert ENV_VALUE not in result.stdout + result.stderr

    def test_install_service_uses_the_fixture_launch_agents_directory(self):
        root = self.base / "repo"
        label = "com.example.weekly-audit"
        relative = owned_plist(root, label)
        document = manifest(entry("weekly-audit", kind="launchd", label=label, plist=relative, installed=False))
        state = self.base / "state"
        state.mkdir()
        log = self.base / "launchctl.log"
        launchctl = stub_launchctl(self.bin, log, state)
        agents = self.base / "LaunchAgents"
        result = self.run_components(
            document,
            "--install-service",
            "weekly-audit",
            "--root",
            str(root),
            "--launch-agents-dir",
            str(agents),
            "--launchctl",
            str(launchctl),
            "--platform",
            "darwin",
        )
        assert result.returncode == 0, result.stdout + result.stderr
        dest = agents / f"{label}.plist"
        assert dest.read_bytes() == (root / relative).read_bytes()
        assert "bootstrap gui/" in log.read_text()

    def test_services_and_install_service_are_exclusive(self):
        result = self.run_components(manifest(), "--services", "--install-service", "x")
        assert result.returncode == 2


WORKER_LABEL = "com.example.worker"
WORKER_PLIST = f"scripts/launchd/{WORKER_LABEL}.plist"
WORKER = entry(
    "worker",
    kind="launchd",
    label=WORKER_LABEL,
    plist=WORKER_PLIST,
    installed=False,
    container=["python3", "scripts/worker.py", "run"],
)


class TestWorkerCheck:
    """A declared, not yet installed service with a container
    command reads as off on the Mac and prints its install command."""

    def test_the_mac_check_reports_it_off_and_prints_the_install_command(self, tmp_path):
        p, _ = probe({})
        findings = components.check_services(manifest(WORKER), p, launch_agents=tmp_path / "agents")
        assert [(f.kind, f.failing) for f in findings] == [("not installed", False)], text(findings)
        assert "--install-service worker" in findings[0].message


class TestContainerDeclaration:
    def test_a_container_command_must_be_a_list_of_strings(self):
        for bad in ("python3 scripts/worker.py run", [], ["python3", ""], ["python3", 3]):
            service = entry("x", kind="launchd", label="com.example.x", container=bad)
            errors = components.validate(manifest(service))
            assert any("container" in e for e in errors), (bad, errors)

    def test_a_container_command_is_accepted(self):
        service = entry("x", kind="launchd", label="com.example.x", container=["python3", "scripts/x.py"])
        assert components.validate(manifest(service)) == []


class TestInstallOffTheMac:
    """--install-service is the Mac's install path. Elsewhere there is no launchd: it refuses before writing anything."""

    def test_install_service_off_darwin_refuses_and_copies_nothing(self, tmp_path):
        root = tmp_path / "repo"
        agents = tmp_path / "LaunchAgents"
        relative = owned_plist(root, WORKER_LABEL)
        service = entry("worker", kind="launchd", label=WORKER_LABEL, plist=relative, installed=False, container=["true"])
        runner = FakeRunner({})
        p = components.Probe(runner=runner, uid=UID)
        stderr = io.StringIO()
        with mock.patch("sys.stderr", stderr):
            code = components.install_service(manifest(service), "worker", root=root, launch_agents=agents, probe=p, platform="linux")
        assert code == 1
        assert not agents.exists()
        assert runner.calls == []
        assert "only on macOS" in stderr.getvalue()


class TestInstallWorkerCommandLine:
    """stratarc.components --install-service worker against a fixture manifest and a fixture plist under a temporary root (--root)."""

    @pytest.fixture(autouse=True)
    def workspace(self, tmp_path, run_env):
        self.base = tmp_path
        self.env = run_env
        self.root = self.base / "repo"
        relative = owned_plist(self.root, WORKER_LABEL)
        self.manifest = self.base / "components.json"
        self.manifest.write_text(json.dumps(manifest(dict(WORKER, plist=relative))), encoding="utf-8")
        self.agents = self.base / "LaunchAgents"

    def run_install(self, *args):
        return subprocess.run(
            [
                sys.executable, "-m", "stratarc.components", "--manifest", str(self.manifest), "--root", str(self.root),
                "--install-service", "worker", "--launch-agents-dir", str(self.agents), *args,
            ],
            capture_output=True,
            text=True,
            timeout=60,
            env=self.env,
        )

    def test_install_service_off_darwin_exits_1_from_the_command_line(self):
        result = self.run_install("--platform", "linux")
        assert result.returncode == 1, result.stdout + result.stderr
        assert "only on macOS" in result.stderr
        assert not self.agents.exists()

    def test_install_service_worker_uses_the_fixture_launch_agents_directory(self):
        bin_dir = self.base / "bin"
        bin_dir.mkdir()
        state = self.base / "state"
        state.mkdir()
        log = self.base / "launchctl.log"
        launchctl = stub_launchctl(bin_dir, log, state)
        result = self.run_install("--launchctl", str(launchctl), "--platform", "darwin")
        assert result.returncode == 0, result.stdout + result.stderr
        dest = self.agents / f"{WORKER_LABEL}.plist"
        assert dest.read_bytes() == (self.root / f"scripts/launchd/{WORKER_LABEL}.plist").read_bytes()
        assert f"bootstrap gui/{os.getuid()} {dest}" in log.read_text()
        assert (self.agents / f".{WORKER_LABEL}.installed").is_file()
