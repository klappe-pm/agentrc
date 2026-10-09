"""The documentation site builder: pages, anchors, links, size, and the drift check on generated pages."""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path

import pytest

from stratarc import messages

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "docs" / "build_site.py"
GENERATED = Path("docs") / "reference" / "generated"
LOAD_PAGE_BUDGET = 50 * 1024
PAGE_BUDGET = 300 * 1024


def _load_builder():
    spec = importlib.util.spec_from_file_location("build_site", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["build_site"] = module
    spec.loader.exec_module(module)
    return module


class _Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.links: list[str] = []
        self.resources: list[tuple[str, str]] = []
        self.tags: list[str] = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        self.tags.append(tag)
        if "id" in values:
            self.ids.add(values["id"])
        if tag == "a" and "href" in values:
            self.links.append(values["href"])
        if tag == "link" and "href" in values:
            self.resources.append((tag, values["href"]))
        for name in ("src", "srcset", "data"):
            if name in values:
                self.resources.append((tag, values[name]))


def _parse(path: Path) -> _Page:
    page = _Page()
    page.feed(path.read_text(encoding="utf-8"))
    return page


@pytest.fixture(scope="module")
def site(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("site") / "site"
    result = subprocess.run([sys.executable, str(SCRIPT), "--out", str(out)], cwd=REPO, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return out


def test_the_expected_pages_exist(site: Path):
    for rel in (
        "index.html",
        "load.html",
        "site.css",
        "docs/index.html",
        "docs/tutorials/getting-started.html",
        "docs/reference/generated/commands.html",
        "docs/reference/generated/errors.html",
        "developer-docs/explanation/cli-design.html",
    ):
        assert (site / rel).is_file(), rel


def test_every_source_page_is_built(site: Path):
    sources = [p for section in ("docs", "developer-docs") for p in (REPO / section).rglob("*.md") if "site-source" not in p.parts]
    for source in sources:
        rel = source.relative_to(REPO)
        built = rel.parent / "index.html" if rel.name == "README.md" else rel.with_suffix(".html")
        assert (site / built).is_file(), built


def test_landing_leads_with_a_captured_run_then_install_then_the_tutorial(site: Path):
    text = (site / "index.html").read_text(encoding="utf-8")
    capture = text.index("decided by:")
    install = text.index('id="install"')
    tutorial = text.index('id="your-first-source-root"')
    assert capture < install < tutorial
    assert "$ stratarc config explain permissions.defaultMode --project notes-cli" in text
    assert "permissions.json:4" in text
    assert "illustrative" not in text.lower()


def test_every_message_id_is_anchored(site: Path):
    page = _parse(site / "docs" / "reference" / "generated" / "errors.html")
    for message_id in messages.CATALOG:
        assert message_id in page.ids, message_id
    commands = _parse(site / "docs" / "reference" / "generated" / "commands.html")
    for command in ("init", "sync", "config", "config-explain", "exit-statuses"):
        assert command in commands.ids, command


def test_every_internal_link_resolves(site: Path):
    checked = 0
    pages = sorted(site.rglob("*.html"))
    parsed = {path: _parse(path) for path in pages}
    for path, page in parsed.items():
        for href in page.links:
            if "://" in href or href.startswith("mailto:"):
                continue
            target, _, fragment = href.partition("#")
            destination = path if not target else (path.parent / target).resolve()
            assert destination.is_file(), f"{path.relative_to(site)} links to {href}"
            if fragment and destination.suffix == ".html":
                assert fragment in parsed[destination].ids, f"{path.relative_to(site)} links to missing anchor {href}"
            checked += 1
    assert checked > 100


def test_no_external_requests_and_no_script(site: Path):
    for path in site.rglob("*.html"):
        page = _parse(path)
        assert "script" not in page.tags, path
        assert "iframe" not in page.tags, path
        for tag, value in page.resources:
            assert "://" not in value and not value.startswith("//"), f"{path.name}: {tag} loads {value}"
    for path in list(site.rglob("*.css")) + list(site.rglob("*.html")):
        text = path.read_text(encoding="utf-8")
        assert "@import" not in text, path
        assert "url(http" not in text, path
    assert "/" + "Users" + "/" not in "".join(p.read_text(encoding="utf-8") for p in site.rglob("*.html"))


def test_the_site_follows_the_system_theme_and_fits_a_phone(site: Path):
    css = (site / "site.css").read_text(encoding="utf-8")
    assert "prefers-color-scheme: dark" in css
    page = (site / "index.html").read_text(encoding="utf-8")
    assert 'name="viewport"' in page and 'name="color-scheme"' in page


def test_load_page_is_static_and_under_budget(site: Path):
    load = site / "load.html"
    assert load.stat().st_size < LOAD_PAGE_BUDGET
    text = load.read_text(encoding="utf-8")
    assert "<script" not in text
    assert "@keyframes" not in text and "animation" not in text and "transition" not in text
    assert "<h1>stratarc</h1>" in text
    assert text.count("<li>") == 3
    assert not (set(_parse(load).resources))


def test_every_page_is_under_the_size_budget(site: Path):
    for path in site.rglob("*"):
        if path.is_file():
            assert path.stat().st_size < PAGE_BUDGET, path


def test_committed_generated_pages_match_the_code():
    result = subprocess.run([sys.executable, str(SCRIPT), "--check"], cwd=REPO, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_check_fails_with_exit_6_after_a_generated_page_is_edited(tmp_path: Path):
    shutil.copytree(REPO / GENERATED, tmp_path / GENERATED)
    clean = subprocess.run([sys.executable, str(SCRIPT), "--check", "--repo", str(tmp_path)], capture_output=True, text=True)
    assert clean.returncode == 0, clean.stderr

    edited = tmp_path / GENERATED / "errors.md"
    edited.write_text(edited.read_text(encoding="utf-8").replace("msg-1001", "msg-9999", 1), encoding="utf-8")
    stale = subprocess.run([sys.executable, str(SCRIPT), "--check", "--repo", str(tmp_path)], capture_output=True, text=True)
    assert stale.returncode == 6
    assert "errors.md" in stale.stderr

    (tmp_path / GENERATED / "commands.md").unlink()
    missing = subprocess.run([sys.executable, str(SCRIPT), "--check", "--repo", str(tmp_path)], capture_output=True, text=True)
    assert missing.returncode == 6
    assert "commands.md" in missing.stderr


def test_write_generated_repairs_a_stale_page(tmp_path: Path):
    shutil.copytree(REPO / GENERATED, tmp_path / GENERATED)
    (tmp_path / GENERATED / "errors.md").write_text("stale\n", encoding="utf-8")
    fixed = subprocess.run([sys.executable, str(SCRIPT), "--write-generated", "--repo", str(tmp_path)], capture_output=True, text=True)
    assert fixed.returncode == 0, fixed.stderr
    again = subprocess.run([sys.executable, str(SCRIPT), "--check", "--repo", str(tmp_path)], capture_output=True, text=True)
    assert again.returncode == 0


def test_a_missing_repository_is_invalid_input(tmp_path: Path):
    result = subprocess.run([sys.executable, str(SCRIPT), "--check", "--repo", str(tmp_path / "absent")], capture_output=True, text=True)
    assert result.returncode == 2


def test_a_broken_link_fails_the_build(tmp_path: Path):
    builder = _load_builder()
    repo = tmp_path / "repo"
    (repo / "docs" / "site-source").mkdir(parents=True)
    (repo / "docs" / "site-source" / "index.md").write_text("# home\n\n[gone](../missing.md)\n", encoding="utf-8")
    site = builder.Site(repo, tmp_path / "out")
    with pytest.raises(builder.BuildError, match="does not resolve"):
        site.build()
