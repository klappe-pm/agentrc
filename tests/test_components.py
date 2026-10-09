"""Tests for agentrc.components, the loader and checker for components.json.

The manifest declares the sections and the entry shape; a loadout may name only what it declares. Every test runs on a fixture document or on tests/fixtures/components/manifest.json, never on a live manifest.
"""

from __future__ import annotations

import json
import plistlib
import shutil
from pathlib import Path
from unittest import mock

import pytest

from agentrc import components
from agentrc.adapters import _common

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FIXTURE_DIR = FIXTURES / "components"
FIXTURE_MANIFEST = FIXTURE_DIR / "manifest.json"
SIXTH_ADAPTER = FIXTURES / "adapters" / "registry" / "sixth.py"


def fixture_adapters_with_a_sixth(directory: Path) -> Path:
    """A copy of the packaged adapter files plus the registry fixture's sixth adapter."""
    fixture = directory / "adapters"
    fixture.mkdir()
    packaged = Path(_common.__file__).parent
    for path in packaged.glob("*.py"):
        shutil.copyfile(path, fixture / path.name)
    shutil.copyfile(SIXTH_ADAPTER, fixture / "sixth.py")
    return fixture


def server(name, **extra):
    entry = {"name": name, "runtimes": ["claude", "codex"], "owner": "example", "wanted": True, "command": "npx", "args": ["-y", name]}
    entry.update(extra)
    return entry


class TestFixtureManifest:
    """The fixture manifest, grown from the example, carries one entry of every section and every top-level block."""

    @pytest.fixture(autouse=True)
    def document(self):
        self.document = components.load(FIXTURE_MANIFEST)

    def entry(self, section, name):
        found = [e for e in self.document.get(section) or [] if e.get("name") == name]
        assert len(found) == 1, f"{section} declares {name} once"
        return found[0]

    def test_it_validates(self):
        assert components.validate(self.document) == []

    def test_every_section_is_represented(self):
        for section in components.SECTIONS:
            assert self.document.get(section), section

    def test_a_dependency_carries_a_pinned_version_and_an_image_source(self):
        ripgrep = self.entry("dependencies", "ripgrep")
        assert ripgrep["version"] and ripgrep["image"]["method"] == "apt"

    def test_a_declared_launchd_service_is_not_yet_installed(self):
        embeddings = self.entry("services", "embeddings")
        assert embeddings["wanted"] is True
        assert embeddings["installed"] is False
        assert embeddings["kind"] == "launchd"

    def test_every_declared_plist_exists_and_carries_its_declared_label(self):
        """install-service refuses a plist whose Label differs, so a declaration that would only fail at install time is a defect in the manifest."""
        declared = [e for e in self.document["services"] if e.get("plist")]
        assert declared
        for entry in declared:
            path = FIXTURE_DIR / entry["plist"]
            assert path.is_file(), entry["plist"]
            with path.open("rb") as handle:
                assert plistlib.load(handle).get("Label") == entry["label"], entry["plist"]

    def test_the_unwanted_service_names_the_match_and_removal_that_find_it(self):
        app = self.entry("services", "vendor-app")
        assert app["wanted"] is False
        assert app["match"] and app["remove"]

    def test_environments_deploy_named_refs(self):
        assert self.document["environments"]["container"]["ref"] == "main"
        assert self.document["environments"]["mac"]["ref"] == "stable"

    def test_the_promotion_block_declares_a_high_and_a_low_risk_tier(self):
        promotion = self.document["promotion"]
        tiers = promotion["risk_tiers"]
        assert tiers["high"]["soak_days"] > components.DEFAULT_SOAK_DAYS
        assert tiers["low"]["documentation"]
        assert "hooks/" in tiers["high"]["paths"]

    def test_the_platform_difference_covers_launchd(self):
        by_name = {entry["name"]: entry for entry in self.document["platform_differences"]}
        assert by_name["launchd"]["platform"] == "darwin"
        assert "home:Library/LaunchAgents/*" in by_name["launchd"]["paths"]

    def test_runtime_settings_carry_the_status_line(self):
        assert "statusLine" in self.document["runtime_settings"]["claude"]["settings"]


class TestValidate:
    def test_load_reads_the_source_roots_manifest_by_default(self, source_root):
        shutil.copyfile(FIXTURE_MANIFEST, source_root / "components.json")
        assert components.load()["version"] == 1
        assert components.validate(components.load()) == []

    def test_a_well_formed_manifest_has_no_errors(self):
        document = {"version": 1, "budgets": {}, "mcp_servers": [server("github", env={"GITHUB_TOKEN": "secret://github/default"})]}
        assert components.validate(document) == []

    def test_a_fixture_manifest_written_to_disk_loads_and_validates(self, tmp_path):
        """A manifest carrying one entry of every section plus every top-level
        block, written to a temporary directory and read back through load():
        the validator's full path."""
        document = {
            "version": 1,
            "budgets": {},
            "runtime_settings": {"claude": {"settings": {"statusLine": {"type": "command"}}}},
            "platform_differences": [{"name": "launchd", "description": "the Mac schedules jobs with launchd", "platform": "darwin", "paths": ["home:Library/LaunchAgents/*"]}],
            "environments": {"container": {"ref": "main"}, "mac": {"ref": "stable"}},
            "promotion": {"soak_days": 3, "risk_tiers": {"high": {"soak_days": 5, "paths": ["hooks/"]}, "low": {"soak_days": 0, "paths": ["docs/"], "documentation": True}}},
            "autonomy": {"concurrency": 2},
            **{section: [entry] for section, entry in VALID.items()},
        }
        path = tmp_path / "components.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        loaded = components.load(path)
        assert components.validate(loaded) == []
        assert sorted(components.declared(loaded, "services")) == ["embeddings"]
        assert components.soak_days(loaded) == 3

    def test_entry_problems_are_named(self):
        document = {
            "version": 1,
            "mcp_servers": [
                {"name": "a", "runtimes": ["claude"], "owner": "x", "wanted": True},
                server("b", runtimes=["emacs"]),
                server("b"),
                server("c", wanted="yes"),
            ],
            "gadgets": [],
        }
        joined = "\n".join(components.validate(document))
        assert "mcp_servers a: needs a command or a url" in joined
        assert "unknown runtime emacs" in joined
        assert "declared twice" in joined
        assert "wanted" in joined
        assert "unknown section gadgets" in joined

    def test_a_literal_secret_in_env_is_refused(self):
        literal = "sk-" + "a" * 40
        document = {"version": 1, "mcp_servers": [server("leaky", env={"API_KEY": literal})]}
        errors = components.validate(document)
        assert any("secret://" in e for e in errors), errors
        # The value itself never appears in a message.
        assert not any(literal in e for e in errors)

    def test_budget_errors_come_through(self):
        errors = components.validate({"version": 1, "budgets": {"turns": -3}})
        assert any(e.startswith("budgets:") for e in errors), errors


def common(**extra):
    entry = {"name": "x", "runtimes": ["claude"], "owner": "example", "wanted": True}
    entry.update(extra)
    return entry


VALID = {
    "mcp_servers": server("github"),
    "plugins": common(name="superpowers", marketplace="claude-plugins-official"),
    "foreign_hooks": common(name="orca", owner="orca", wanted=False, match="orca-hook"),
    "third_party_skills": common(name="impeccable", owner="impeccable", installer="npx impeccable install"),
    "services": common(name="embeddings", kind="launchd", label="com.example.embeddings", port=11434, plist="scripts/launchd/com.example.embeddings.plist"),
    "dependencies": common(name="ripgrep", command="rg", install="brew install ripgrep", version="14.1.1", image={"method": "apt", "package": "ripgrep"}),
}


class TestSectionSchemas:
    """Every section has a checked shape, and an unknown key is an error so a typo
    cannot silently disable a check."""

    def errors(self, section, entry):
        return "\n".join(components.validate({"version": 1, section: [entry]}))

    def test_one_well_formed_entry_of_every_section_is_valid(self):
        for section, entry in VALID.items():
            assert self.errors(section, entry) == ""

    def test_a_service_without_a_label_is_refused(self):
        entry = dict(VALID["services"])
        del entry["label"]
        assert "services embeddings: needs label" in self.errors("services", entry)

    def test_a_service_kind_must_be_launchd_or_app(self):
        assert "kind must be launchd or app" in self.errors("services", dict(VALID["services"], kind="cron"))

    def test_a_service_port_must_be_a_port_number(self):
        assert "port must be" in self.errors("services", dict(VALID["services"], port="11434"))
        assert "port must be" in self.errors("services", dict(VALID["services"], port=70000))

    def test_an_unwanted_service_needs_match_and_remove(self):
        entry = common(name="vendor-app", owner="Vendor.app", wanted=False, kind="app", label="Vendor", port=11434)
        joined = self.errors("services", entry)
        assert "needs match" in joined
        assert "needs remove" in joined
        entry.update(match="/Applications/Vendor.app/Contents/MacOS/Vendor", remove="osascript -e 'quit app \"Vendor\"'")
        assert self.errors("services", entry) == ""

    def test_the_checked_in_installed_flag_is_part_of_the_service_shape(self):
        assert self.errors("services", dict(VALID["services"], installed=False)) == ""
        assert "installed must be true or false" in self.errors("services", dict(VALID["services"], installed="no"))

    def test_a_service_environment_default_must_be_a_string_map(self):
        assert self.errors("services", dict(VALID["services"], environment={"HOST_BIND": "127.0.0.1"})) == ""
        assert "environment must be an object of string environment variable defaults" in self.errors("services", dict(VALID["services"], environment=["HOST_BIND=127.0.0.1"]))
        assert "environment HOST_BIND must be a string" in self.errors("services", dict(VALID["services"], environment={"HOST_BIND": True}))

    def test_a_service_environment_default_cannot_be_a_secret_reference(self):
        # A service's environment block is checked into git; a secret:// value
        # there would leak the reference and defeat the credential profiles,
        # which are the only place a service's secrets are declared.
        assert "environment ENCRYPTION_KEY must not be a secret:// reference" in self.errors("services", dict(VALID["services"], environment={"ENCRYPTION_KEY": "secret://example/encryption-key"}))

    def test_a_service_environment_default_cannot_be_token_shaped(self):
        literal = "sk-" + "a" * 40
        assert "environment API_KEY holds a token-shaped literal" in self.errors("services", dict(VALID["services"], environment={"API_KEY": literal}))

    def test_a_service_image_must_be_pinned_to_a_tag_or_digest(self):
        # A launchd job that starts a Docker container names its image here; "latest"
        # or an untagged reference is refused so the pin cannot silently drift.
        assert self.errors("services", dict(VALID["services"], image="ghcr.io/example/thing:v1.2.3")) == ""
        assert (self.errors(
                "services",
                dict(VALID["services"], image="ghcr.io/example/thing@sha256:" + "a" * 64),
            )) == ""
        assert "image must be pinned to a version tag or digest, not latest" in self.errors("services", dict(VALID["services"], image="ghcr.io/example/thing:latest"))
        assert "image must be pinned to a version tag or digest" in self.errors("services", dict(VALID["services"], image="ghcr.io/example/thing"))
        assert "image must be a string" in self.errors("services", dict(VALID["services"], image=True))

    def test_a_dependency_without_install_is_refused(self):
        entry = dict(VALID["dependencies"])
        del entry["install"]
        assert "dependencies ripgrep: needs install" in self.errors("dependencies", entry)

    def test_a_dependency_without_command_is_refused(self):
        entry = dict(VALID["dependencies"])
        del entry["command"]
        assert "dependencies ripgrep: needs command" in self.errors("dependencies", entry)

    def test_a_dependency_without_a_pinned_version_is_refused(self):
        # The image pins every tool from this list, so an unpinned entry cannot be built.
        entry = dict(VALID["dependencies"])
        del entry["version"]
        assert "dependencies ripgrep: needs version" in self.errors("dependencies", entry)

    def test_a_dependency_without_an_image_source_is_refused(self):
        entry = dict(VALID["dependencies"])
        del entry["image"]
        assert "dependencies ripgrep: needs image" in self.errors("dependencies", entry)

    def test_a_dependency_platform_is_optional_and_must_be_darwin_or_linux(self):
        # A keyring daemon is a container-only (Linux) addition; every other
        # dependency carries no platform key and applies everywhere.
        assert self.errors("dependencies", VALID["dependencies"]) == ""
        assert self.errors("dependencies", dict(VALID["dependencies"], platform="linux")) == ""
        assert "dependencies ripgrep: platform must be darwin or linux" in self.errors("dependencies", dict(VALID["dependencies"], platform="container"))

    def test_every_image_method_has_its_own_shape(self):
        valid = {
            "base": {"method": "base", "from": "python:{version}-slim-trixie"},
            "apt": {"method": "apt", "package": "tmux"},
            "npm": {"method": "npm", "package": "@openai/codex"},
            "release": {"method": "release", "url": "https://example.test/v{version}/x-{arch}.tar.gz", "extract": "tar", "arch": {"amd64": "x64", "arm64": "arm64"}},
            "source": {"method": "source", "url": "https://example.test/git-{version}.tar.xz", "sha256": "a" * 64},
            "release-flat": {"method": "release", "url": "https://example.test/v{version}/x_{arch}.tar.gz", "extract": "tar-flat"},
            # A release zip may hold one directory above the binary
            # (tool-linux-x64/tool), unlike a flat zip, so extraction must
            # strip that wrapper the way tar's --strip-components=1 does.
            "release-zip-strip": {"method": "release", "url": "https://example.test/v{version}/tool-linux-{arch}.zip", "extract": "zip-strip", "arch": {"amd64": "x64", "arm64": "aarch64"}},
            # A release archive may hold the bare executable under a name other
            # than the command it is installed as, so the image renames it
            # after unpack.
            "release-binary": {"method": "release", "url": "https://example.test/v{version}/tool_cli_linux_{arch}.tar.gz", "extract": "tar-flat", "binary": "tool-cli"},
        }
        for method, image in valid.items():
            assert self.errors("dependencies", dict(VALID["dependencies"], image=image)) == ""
        cases = {
            "image method must be one of": {"method": "curl"},
            "image needs from": {"method": "base"},
            "image needs package": {"method": "npm"},
            "image url must carry {version}": {"method": "release", "url": "https://example.test/x.tar.gz", "extract": "tar"},
            "image extract must be tar, tar-flat, zip or zip-strip": {"method": "release", "url": "https://example.test/{version}", "extract": "rar"},
            "image arch must map amd64 and arm64": {"method": "release", "url": "https://example.test/{version}", "extract": "zip", "arch": {"amd64": "x64"}},
            "image unknown key pakage": {"method": "apt", "package": "tmux", "pakage": "tmux"},
            # A source tarball is compiled in the image, so it is verified
            # against a pinned checksum before anything runs from it.
            "image sha256 must be 64 lowercase hex digits": {"method": "source", "url": "https://example.test/{version}.tar.xz", "sha256": "ABC"},
            "image source needs sha256": {"method": "source", "url": "https://example.test/{version}.tar.xz"},
            "image unknown key arch": {"method": "source", "url": "https://example.test/{version}.tar.xz", "sha256": "a" * 64, "arch": {"amd64": "x64", "arm64": "arm64"}},
            "image binary must name the extracted executable": {"method": "release", "url": "https://example.test/{version}", "extract": "tar-flat", "binary": ""},
            "image must be an object": "apt-get install tmux",
        }
        for message, image in cases.items():
            assert message in self.errors("dependencies", dict(VALID["dependencies"], image=image))
        unversioned = {"method": "source", "url": "https://example.test/x.tar.xz", "sha256": "a" * 64}
        assert "image url must carry {version}" in self.errors("dependencies", dict(VALID["dependencies"], image=unversioned))

    def test_a_release_may_pin_a_sha256_per_architecture(self):
        """A release archive differs per architecture, so its checksum is a
        map from each image architecture to that archive's sha256, verified
        in the image before the archive is unpacked."""
        release = {"method": "release", "url": "https://example.test/v{version}/x_{arch}.tar.gz", "extract": "tar-flat"}
        pinned = dict(release, sha256={"amd64": "a" * 64, "arm64": "b" * 64})
        assert self.errors("dependencies", dict(VALID["dependencies"], image=pinned)) == ""
        message = "image release sha256 must map amd64 and arm64 to 64 lowercase hex digits"
        for bad in ("a" * 64, {"amd64": "a" * 64}, {"amd64": "a" * 64, "arm64": "B" * 64}, {"amd64": "a" * 64, "arm64": "b" * 64, "riscv64": "c" * 64}, {"amd64": 1, "arm64": "b" * 64}):
            assert message in self.errors("dependencies", dict(VALID["dependencies"], image=dict(release, sha256=bad)))

    def test_a_version_command_is_a_string(self):
        assert self.errors("dependencies", dict(VALID["dependencies"], version_command="rg --version")) == ""
        assert "version_command must be a command string" in self.errors("dependencies", dict(VALID["dependencies"], version_command=["rg"]))

    def test_a_foreign_hook_without_match_is_refused(self):
        entry = dict(VALID["foreign_hooks"])
        del entry["match"]
        assert "foreign_hooks orca: needs match" in self.errors("foreign_hooks", entry)

    def test_a_third_party_skill_without_installer_is_refused(self):
        entry = dict(VALID["third_party_skills"])
        del entry["installer"]
        assert "third_party_skills impeccable: needs installer" in self.errors("third_party_skills", entry)

    def test_a_wanted_foreign_hook_may_carry_the_registration_an_adapter_renders(self):
        # A wanted foreign hook is not always the source root's to render: one
        # it only recognizes when another tool installs it (found by match,
        # kept by prune) carries no registration, and that is not an error,
        # so validate must still accept a wanted foreign hook with no
        # registration or prune would refuse any manifest that has one.
        wanted = common(name="reviewer-agent-hook", owner="acme", match="reviewer agent-hook run", runtimes=["claude", "codex"])
        assert self.errors("foreign_hooks", wanted) == ""
        good = dict(wanted, registration={"claude": "scripts/reviewer/claude.json", "codex": "scripts/reviewer/codex.json"})
        assert self.errors("foreign_hooks", good) == ""

    def test_an_unwanted_foreign_hook_needs_no_registration(self):
        assert self.errors("foreign_hooks", VALID["foreign_hooks"]) == ""

    def test_a_registration_names_only_runtimes_the_entry_declares(self):
        entry = common(name="reviewer-agent-hook", owner="acme", match="reviewer agent-hook run", runtimes=["claude"])
        unknown = dict(entry, registration={"claude": "scripts/reviewer/claude.json", "emacs": "scripts/reviewer/emacs.json"})
        assert "registration: unknown runtime emacs" in self.errors("foreign_hooks", unknown)
        undeclared = dict(entry, registration={"claude": "scripts/reviewer/claude.json", "codex": "scripts/reviewer/codex.json"})
        assert "registration: codex is not in runtimes" in self.errors("foreign_hooks", undeclared)

    def test_a_registration_maps_each_runtime_to_a_path(self):
        entry = common(name="reviewer-agent-hook", owner="acme", match="reviewer agent-hook run", runtimes=["claude"])
        assert "registration must be an object" in self.errors("foreign_hooks", dict(entry, registration="scripts/reviewer/claude.json"))
        assert "registration claude must name the captured file" in self.errors("foreign_hooks", dict(entry, registration={"claude": ""}))

    def test_a_third_party_skill_may_name_the_directories_it_installs(self):
        # A tool may install several skill directories under one entry, so the
        # entry names them and the adapters' inventory matches any of them.
        entry = common(name="reviewer", owner="acme", installer="reviewer skills install", skills=["reviewer-review", "reviewer-fix"])
        assert self.errors("third_party_skills", entry) == ""
        assert "skills must be a list of directory names" in self.errors("third_party_skills", dict(entry, skills="reviewer-review"))
        assert "skills must be a list of directory names" in self.errors("third_party_skills", dict(entry, skills=["reviewer-review", ""]))

    def test_a_plugin_needs_a_marketplace(self):
        entry = dict(VALID["plugins"])
        del entry["marketplace"]
        assert "plugins superpowers: needs marketplace" in self.errors("plugins", entry)

    def test_an_opencode_file_plugin_carries_a_path_instead(self):
        entry = common(name="notify", runtimes=["opencode"], path="plugins/notify.ts")
        assert self.errors("plugins", entry) == ""
        assert "path is only for OpenCode file plugins" in self.errors("plugins", dict(entry, runtimes=["claude", "opencode"]))

    def test_a_hosted_mcp_server_needs_no_command_and_hosted_is_a_boolean(self):
        hosted = common(name="gmail", hosted=True)
        assert self.errors("mcp_servers", hosted) == ""
        assert "hosted must be true or false" in self.errors("mcp_servers", server("github", hosted="yes"))

    def test_a_rendered_mcp_server_name_is_a_bare_key(self):
        """The adapters write the name as a TOML table key
        ([mcp_servers.<name>]) and a JSON key; a name with a dot, space or
        bracket would name a different table. A hosted connector is never
        rendered, so its name is the provider's."""
        assert self.errors("mcp_servers", server("my-server_2")) == ""
        for bad in ("my.server", "my server", "srv]", ""):
            if bad:
                assert "name must be letters, digits, - or _" in self.errors("mcp_servers", server(bad))
        assert self.errors("mcp_servers", common(name="claude.ai Gmail", hosted=True)) == ""

    def test_an_unknown_key_is_an_error_in_every_section(self):
        for section, entry in VALID.items():
            joined = self.errors(section, dict(entry, wnated=False))
            assert "unknown key wnated" in joined

    def test_runtime_settings_are_keyed_by_a_known_runtime(self):
        joined = "\n".join(components.validate({"version": 1, "runtime_settings": {"emacs": {}}}))
        assert "runtime_settings: unknown runtime emacs" in joined
        assert components.validate({"version": 1, "runtime_settings": {"claude": {}}}) == []


class TestRuntimeSettingsSchema:
    """runtime_settings.<runtime> accepts exactly model, subagent_model, aliases and settings (plus environment_plugins for claude and provider for opencode)."""

    def errors(self, block):
        return "\n".join(components.validate({"version": 1, "runtime_settings": {"codex": block}}))

    def test_the_four_keys_are_accepted(self):
        block = {
            "model": "gpt-5",
            "subagent_model": "gpt-5-mini",
            "aliases": {"opus": "gpt-5", "sonnet": "gpt-5-mini"},
            "settings": {"anything": {"nested": True}},
        }
        assert self.errors(block) == ""

    def test_an_unknown_key_is_rejected(self):
        assert "runtime_settings codex: unknown key statusLine" in self.errors({"statusLine": {}})

    def test_models_must_be_nonempty_strings(self):
        for key in ("model", "subagent_model"):
            for bad in ("", 5, ["opus"]):
                assert f"runtime_settings codex: {key} must be a model name" in self.errors({key: bad})

    def test_inheritance_sentinels_are_not_model_names(self):
        for key in ("model", "subagent_model"):
            for sentinel in ("inherit", "default"):
                assert f"runtime_settings codex: {key} must be a model name" in self.errors({key: sentinel})

    def test_aliases_map_names_to_nonempty_strings(self):
        for bad in (["opus"], {"opus": ""}, {"opus": 1}, {"": "gpt-5"}):
            assert "runtime_settings codex: aliases must map each short name to a model name" in self.errors({"aliases": bad})

    def test_settings_must_be_an_object(self):
        assert "runtime_settings codex: settings must be an object" in self.errors({"settings": []})

    def errors_for(self, runtime, block):
        return "\n".join(components.validate({"version": 1, "runtime_settings": {runtime: block}}))

    def test_provider_maps_ids_to_objects_for_opencode(self):
        block = {"provider": {"local-gateway": {"npm": "@ai-sdk/openai-compatible", "options": {"baseURL": "http://127.0.0.1:3001/v1"}}}}
        assert self.errors_for("opencode", block) == ""

    def test_provider_must_map_each_id_to_an_object(self):
        assert "runtime_settings opencode: provider must map each provider id to an object" in self.errors_for("opencode", {"provider": {"local-gateway": ["not", "an", "object"]}})
        assert "runtime_settings opencode: provider must map each provider id to an object" in self.errors_for("opencode", {"provider": ["local-gateway"]})

    def test_provider_is_only_accepted_for_opencode(self):
        assert "runtime_settings codex: unknown key provider" in self.errors_for("codex", {"provider": {}})


class TestPlatformDifferences:
    """A macOS-only assumption the suite hits in a Linux image is fixed or declared here, and a declaration names what it exempts."""

    DIFFERENCE = {
        "name": "caffeinate",
        "description": "The shim wraps /usr/bin/caffeinate, which only macOS ships.",
        "platform": "darwin",
        "tests": ["scripts/caffeinate-shim.test.sh"],
    }

    def errors(self, entries):
        return "\n".join(components.validate({"version": 1, "platform_differences": entries}))

    def test_a_well_formed_difference_is_valid(self):
        assert self.errors([self.DIFFERENCE]) == ""

    def test_a_difference_needs_a_name_and_a_description(self):
        assert "platform_differences: an entry has no name" in self.errors([{"description": "x", "platform": "darwin"}])
        entry = dict(self.DIFFERENCE)
        del entry["description"]
        assert "platform_differences caffeinate: needs description" in self.errors([entry])

    def test_the_platform_is_darwin_or_linux(self):
        assert "platform must be darwin or linux" in self.errors([dict(self.DIFFERENCE, platform="macos")])

    def test_tests_are_a_list_of_repository_paths(self):
        assert "tests must be a list of paths" in self.errors([dict(self.DIFFERENCE, tests="scripts/caffeinate-shim.test.sh")])

    def test_an_unknown_key_and_a_repeat_are_errors(self):
        joined = self.errors([dict(self.DIFFERENCE, platfrom="darwin"), self.DIFFERENCE])
        assert "platform_differences caffeinate: unknown key platfrom" in joined
        assert "platform_differences caffeinate: declared twice" in joined

    def test_the_section_must_be_a_list(self):
        assert "platform_differences must be a list" in self.errors({})

    def test_paths_name_parity_manifest_keys_it_excuses(self):
        """An environment parity check excuses a difference only where a
        declaration names its manifest key, by scope prefix and pattern."""
        entry = dict(self.DIFFERENCE, paths=["home:bin/caffeinate", "runtime:*:hooks/x.sh"])
        assert self.errors([entry]) == ""

    def test_paths_must_be_scoped_manifest_key_patterns(self):
        assert "paths must be a list of manifest key patterns" in self.errors([dict(self.DIFFERENCE, paths="home:bin/caffeinate")])
        joined = self.errors([dict(self.DIFFERENCE, paths=["bin/caffeinate", "*"])])
        assert "platform_differences caffeinate: path bin/caffeinate must start with home:, project: or runtime:" in joined
        assert "platform_differences caffeinate: path * must start with home:, project: or runtime:" in joined


def fixture_platform(entry, label):
    """A deploy platform validator a test registers: it refuses one start command."""
    return [f"{label}: the fixture platform refuses start command {entry['start_command']}"] if entry.get("start_command") == "refused" else []


PLATFORM_MODULE = """\
def fixture_platform(entry, label):
    if entry.get("start_command") == "refused":
        return [f"{label}: the fixture platform refuses start command {entry['start_command']}"]
    return []


DEPLOY_PLATFORMS = {"fixture": fixture_platform}
"""


class TestDeploy:
    """The deploy section declares the hosted service configuration a platform's deploy script plans and checks, with variables by name and every secret a sealed secret:// reference. The platform itself is an extension the source root registers at scripts/private/components_platforms.py."""

    TARGET = {
        "name": "staging",
        "platform": "fixture",
        "description": "The staging container.",
        "service": "example-staging",
        "source": {"image": "ghcr.io/example/example-dev:main"},
        "start_command": "sleep infinity",
        "healthcheck": None,
        "restart_policy": "ALWAYS",
        "serverless": False,
        "replicas": 1,
        "status_port": 8765,
        "variables": [
            {"name": "SERVICE_ACCOUNT_TOKEN", "sealed": True, "secret": "secret://example/host", "description": "Set by hand."},
            {"name": "RUN_UID", "sealed": False, "value": "0"},
        ],
    }

    @pytest.fixture(autouse=True)
    def registered_platform(self, source_root):
        module = source_root / "scripts" / "private" / "components_platforms.py"
        module.parent.mkdir(parents=True)
        module.write_text(PLATFORM_MODULE, encoding="utf-8")
        self.source_root = source_root

    def errors(self, entries):
        return "\n".join(components.validate({"version": 1, "deploy": entries}))

    def target(self, **changes):
        entry = json.loads(json.dumps(self.TARGET))
        entry.update(changes)
        return entry

    def test_a_well_formed_target_is_valid(self):
        assert self.errors([self.TARGET]) == ""

    def test_the_section_must_be_a_list(self):
        assert "deploy must be a list" in self.errors({})

    def test_an_unknown_key_and_a_repeat_are_errors(self):
        joined = self.errors([self.target(start="x"), self.TARGET])
        assert "deploy staging: unknown key start" in joined
        assert "deploy staging: declared twice" in joined

    def test_the_platform_must_be_a_registered_one(self):
        assert "deploy staging: platform must be one of fixture, not fly" in self.errors([self.target(platform="fly")])
        assert "deploy staging: platform must be one of fixture, not none" in self.errors([self.target(platform=None)])
        entry = self.target()
        del entry["platform"]
        assert "deploy staging: platform must be one of fixture, not none" in self.errors([entry])

    def test_the_platforms_validator_findings_come_through(self):
        assert "deploy staging: the fixture platform refuses start command refused" in self.errors([self.target(start_command="refused")])

    def test_the_generic_shape_is_still_checked_on_an_unknown_platform(self):
        joined = self.errors([self.target(platform="fly", replicas=0)])
        assert "platform must be one of fixture, not fly" in joined
        assert "deploy staging: replicas must be a positive integer" in joined

    def test_with_no_platform_registered_every_target_is_refused_naming_the_module(self):
        """A source root without the private platform module knows no platform: a target names one it cannot validate, and the message says which file would register it."""
        (self.source_root / "scripts" / "private" / "components_platforms.py").unlink()
        joined = self.errors([self.target(platform="railway")])
        assert "deploy staging: platform railway has no registered validator; none is registered" in joined
        assert "scripts/private/components_platforms.py" in joined

    def test_check_deploy_takes_an_explicit_registry(self):
        # Callers of _check_deploy get the source root's registry by default
        # and may pass their own.
        assert components._check_deploy([self.TARGET]) == []
        assert components._check_deploy([self.target(platform="other")], platforms={"other": lambda entry, label: []}) == []
        assert "platform must be one of other, not fixture" in "\n".join(components._check_deploy([self.TARGET], platforms={"other": lambda entry, label: []}))

    def test_the_registry_is_read_from_the_source_root_on_each_call(self):
        assert sorted(components.deploy_platforms()) == ["fixture"]
        assert components.private_platforms_path() == self.source_root / "scripts" / "private" / "components_platforms.py"

    def test_the_registry_loads_from_a_private_module_file_when_one_exists(self, tmp_path):
        path = tmp_path / "components_platforms.py"
        assert components.load_deploy_platforms(path) == {}
        path.write_text("DEPLOY_PLATFORMS = {'fly': lambda entry, label: [label + ': fly says no']}\n", encoding="utf-8")
        loaded = components.load_deploy_platforms(path)
        assert sorted(loaded) == ["fly"]
        assert loaded["fly"]({}, "deploy x") == ["deploy x: fly says no"]
        assert components._check_deploy([self.target(platform="fly")], platforms=loaded) == ["deploy staging: fly says no"]

    def test_a_private_module_that_registers_nothing_usable_is_a_defect_not_an_absence(self, tmp_path):
        path = tmp_path / "components_platforms.py"
        for body in ("x = 1\n", "DEPLOY_PLATFORMS = ['fly']\n", "DEPLOY_PLATFORMS = {'fly': 'not callable'}\n", "DEPLOY_PLATFORMS = {'': lambda e, l: []}\n"):
            path.write_text(body, encoding="utf-8")
            with pytest.raises(components.PlatformRegistryError):
                components.load_deploy_platforms(path)

    def test_a_malformed_module_in_the_source_root_fails_validation_loudly(self):
        (self.source_root / "scripts" / "private" / "components_platforms.py").write_text("x = 1\n", encoding="utf-8")
        with pytest.raises(components.PlatformRegistryError):
            self.errors([self.TARGET])

    def test_the_source_is_one_image_or_one_repository(self):
        assert "source must name an image or a repo" in self.errors([self.target(source={})])
        both = {"image": "a:b", "repo": "o/r"}
        assert "source must name an image or a repo, not both" in self.errors([self.target(source=both)])
        assert self.errors([self.target(source={"repo": "o/r", "dockerfile": "deploy/Dockerfile"})]) == ""

    def test_restart_serverless_replicas_and_port_are_checked(self):
        # The restart policy's vocabulary is the platform's; the generic shape
        # only requires one to be named.
        assert "deploy staging: needs restart_policy" in self.errors([self.target(restart_policy="")])
        assert self.errors([self.target(restart_policy="always")]) == ""
        assert "serverless must be true or false" in self.errors([self.target(serverless="no")])
        assert "replicas must be a positive integer" in self.errors([self.target(replicas=0)])
        assert "status_port must be a port number" in self.errors([self.target(status_port=70000)])

    def test_a_healthcheck_is_null_or_a_path(self):
        assert self.errors([self.target(healthcheck={"path": "/healthz", "timeout": 30})]) == ""
        assert "healthcheck must be null or an object with a path" in self.errors([self.target(healthcheck={"timeout": 30})])

    def test_a_sealed_variable_carries_a_secret_reference_and_no_value(self):
        sealed = {"name": "SERVICE_ACCOUNT_TOKEN", "sealed": True, "value": "x"}
        joined = self.errors([self.target(variables=[sealed])])
        assert "variable SERVICE_ACCOUNT_TOKEN: a sealed variable needs a secret:// reference" in joined
        assert "variable SERVICE_ACCOUNT_TOKEN: a sealed variable carries no value" in joined

    def test_a_plain_variable_holding_a_token_shaped_literal_is_refused(self):
        value = "sk-" + "a" * 40
        literal = {"name": "API_KEY", "sealed": False, "value": value}
        joined = self.errors([self.target(variables=[literal])])
        assert "variable API_KEY holds a token-shaped literal" in joined
        assert value not in joined

    def test_a_plain_variable_is_refused_when_the_detector_fails(self):
        value = "safe-fixture"
        literal = {"name": "API_KEY", "sealed": False, "value": value}
        with mock.patch.object(components.subprocess, "run", return_value=mock.Mock(returncode=2)):
            joined = self.errors([self.target(variables=[literal])])
        assert "variable API_KEY holds a token-shaped literal" in joined
        assert value not in joined

    def test_a_variable_name_is_an_environment_name_and_unique(self):
        joined = self.errors([self.target(variables=[{"name": "lower case", "sealed": False, "value": "1"}])])
        assert "variable name must be an environment variable name" in joined
        twice = [{"name": "A", "sealed": False, "value": "1"}, {"name": "A", "sealed": False, "value": "2"}]
        assert "variable A: declared twice" in self.errors([self.target(variables=twice)])


class TestEnvironments:
    """Each environment names the ref it deploys from."""

    def errors(self, environments):
        return "\n".join(components.validate({"version": 1, "environments": environments}))

    def test_a_well_formed_environment_is_valid(self):
        assert self.errors({"mac": {"ref": "stable", "description": "the Mac"}}) == ""

    def test_an_environment_needs_a_ref(self):
        assert "environments mac: needs ref" in self.errors({"mac": {"description": "the Mac"}})
        assert "environments mac: needs ref" in self.errors({"mac": {"ref": ""}})

    def test_an_unknown_key_is_an_error(self):
        assert "environments mac: unknown key branch" in self.errors({"mac": {"ref": "stable", "branch": "stable"}})

    def test_the_section_is_an_object_of_objects(self):
        assert "environments must be an object keyed by environment" in self.errors([])
        assert "environments mac: must be an object" in self.errors({"mac": "stable"})


class TestPromotion:
    """The promotion gate's soak, in days, 3 by default."""

    def errors(self, promotion):
        return "\n".join(components.validate({"version": 1, "promotion": promotion}))

    def test_a_well_formed_section_is_valid(self):
        assert self.errors({"soak_days": 3, "description": "the soak"}) == ""
        assert self.errors({"soak_days": 0.5}) == ""

    def test_soak_days_is_a_non_negative_number(self):
        for bad in (-1, "3", True, None):
            assert "promotion: soak_days must be a number of days, 0 or more" in self.errors({"soak_days": bad})

    def test_an_unknown_key_is_an_error(self):
        assert "promotion: unknown key soak" in self.errors({"soak_days": 3, "soak": 3})

    def test_the_section_is_an_object(self):
        assert "promotion must be an object" in self.errors(3)

    def test_the_default_soak_is_3_days(self):
        assert components.soak_days({}) == components.DEFAULT_SOAK_DAYS
        assert components.DEFAULT_SOAK_DAYS == 3
        assert components.soak_days({"promotion": {"soak_days": 1}}) == 1
        assert components.soak_days({"promotion": {"soak_days": "3"}}) == components.DEFAULT_SOAK_DAYS


class TestRiskTiers:
    """The shape of promotion.risk_tiers."""

    def errors(self, risk_tiers):
        return "\n".join(components.validate({"version": 1, "promotion": {"soak_days": 3, "risk_tiers": risk_tiers}}))

    def test_a_well_formed_tier_map_is_valid(self):
        assert self.errors({"high": {"soak_days": 5, "paths": ["hooks/"]}, "low": {"soak_days": 0, "paths": ["*.md"], "documentation": True}}) == ""

    def test_risk_tiers_is_an_object(self):
        assert "promotion: risk_tiers must be an object keyed by tier name" in self.errors([])

    def test_a_tier_is_an_object(self):
        assert "promotion: risk_tiers high: must be an object" in self.errors({"high": "high"})

    def test_a_tier_needs_a_valid_soak_days(self):
        for bad in (-1, "5", True, None):
            assert "promotion: risk_tiers high: soak_days must be a number of days, 0 or more" in self.errors({"high": {"soak_days": bad, "paths": ["hooks/"]}})

    def test_a_tier_needs_a_non_empty_list_of_paths(self):
        for bad in ([], "hooks/", [""], None):
            assert "promotion: risk_tiers high: paths must be a non-empty list of strings" in self.errors({"high": {"soak_days": 5, "paths": bad}})

    def test_documentation_must_be_a_bool(self):
        assert "promotion: risk_tiers low: documentation must be true or false" in self.errors({"low": {"soak_days": 0, "paths": ["*.md"], "documentation": "yes"}})

    def test_only_one_tier_may_be_documentation(self):
        errors = self.errors({
            "high": {"soak_days": 5, "paths": ["hooks/"], "documentation": True},
            "low": {"soak_days": 0, "paths": ["*.md"], "documentation": True},
        })
        assert "promotion: risk_tiers: only one tier may set documentation: true" in errors

    def test_an_unknown_key_in_a_tier_is_an_error(self):
        assert "promotion: risk_tiers high: unknown key floor" in self.errors({"high": {"soak_days": 5, "paths": ["hooks/"], "floor": 1}})


class TestSoakOverride:
    """The plain soak override a project's promotion.json and a
    loadout's promotion key share; no risk_tiers here."""

    def test_a_well_formed_override_is_valid(self):
        assert components._check_soak_override({"soak_days": 2}, "promotion.json") == []
        assert components._check_soak_override({"soak_days": 2, "description": "why"}, "promotion.json") == []

    def test_the_value_is_an_object(self):
        assert components._check_soak_override(2, "promotion.json") == ["promotion.json must be an object"]

    def test_soak_days_is_a_non_negative_number(self):
        for bad in (-1, "2", True, None):
            assert "promotion.json: soak_days must be a number of days, 0 or more" in components._check_soak_override({"soak_days": bad}, "promotion.json")

    def test_risk_tiers_is_not_an_allowed_key(self):
        assert "promotion.json: unknown key risk_tiers" in components._check_soak_override({"soak_days": 2, "risk_tiers": {}}, "promotion.json")


class TestRuntimeRegistry:
    """components reads the adapters' runtime registry instead of its own list."""

    def test_the_runtimes_are_the_registry(self):
        assert sorted(components.known_runtimes()) == sorted(_common.runtime_registry())

    def test_a_sixth_adapter_is_accepted_without_other_edits(self, tmp_path):
        document = {"version": 1, "mcp_servers": [server("github", runtimes=["sixth"])], "runtime_settings": {"sixth": {}}}
        fixture = fixture_adapters_with_a_sixth(tmp_path)
        assert components.validate(document, adapters_dir=fixture) == []
        assert "unknown runtime sixth" in "\n".join(components.validate(document))


class TestDeclared:
    def test_only_wanted_entries_for_the_runtime_are_declared(self):
        document = {
            "version": 1,
            "mcp_servers": [server("one"), server("two", wanted=False), server("three", runtimes=["gemini"])],
        }
        assert sorted(components.declared(document, "mcp_servers")) == ["one", "three"]
        assert sorted(components.declared(document, "mcp_servers", runtime="codex")) == ["one"]

    def test_a_missing_file_loads_as_an_empty_manifest(self, tmp_path):
        document = components.load(tmp_path / "components.json")
        assert components.declared(document, "mcp_servers") == {}

    def test_a_malformed_file_is_an_error_not_an_empty_manifest(self, tmp_path):
        path = tmp_path / "components.json"
        path.write_text("{not json")
        with pytest.raises(components.ManifestError):
            components.load(path)

    def test_a_source_root_with_no_manifest_loads_as_empty(self, source_root):
        assert components.load() == {"version": 1}


class TestAutonomy:
    """The dispatcher's limits section: validated here, defaulted by autonomy_limits."""

    def test_a_good_block_and_an_absent_one_pass(self):
        good = {"version": 1, "autonomy": {"concurrency": 2, "per_project": 1, "interval_minutes": 10, "daily_tokens": None}}
        assert [e for e in components.validate(good) if "autonomy" in e] == []
        assert components.validate_autonomy(None) == []
        assert components.autonomy_limits(None) == {"concurrency": 2, "per_project": 1, "interval_minutes": 10, "daily_tokens": None}
        assert components.autonomy_limits({"concurrency": 5, "daily_tokens": 100})["concurrency"] == 5

    def test_each_bad_value_is_named(self):
        for block, phrase in (
            ({"concurrency": 0}, "autonomy: concurrency must be a whole number of at least 1"),
            ({"per_project": "one"}, "autonomy: per_project must be a whole number of at least 1"),
            ({"interval_minutes": True}, "autonomy: interval_minutes must be a whole number of at least 1"),
            ({"daily_tokens": -5}, "autonomy: daily_tokens must be a whole number of at least 1, or null for no cap"),
            ({"concurrency": None}, "autonomy: concurrency must be a whole number of at least 1"),
            ({"extra": 1}, "autonomy: unknown key extra"),
            (["concurrency"], "autonomy must be an object"),
        ):
            problems = components.validate_autonomy(block)
            assert len(problems) == 1, problems
            assert phrase in problems[0]
