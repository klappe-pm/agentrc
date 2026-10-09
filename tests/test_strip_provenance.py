from __future__ import annotations

from pathlib import Path

from stratarc.strip_provenance import main, strip_provenance

WITH_KEYS = """---
domain: engineering
models:
  - model-a
  - model-b
date-created: 2026-01-01
date-revised: 2026-10-08
providers: [vendor-a, vendor-b]
status: active
session-link: https://example.invalid/session/abc
tags:
  - alpha
  - beta
---

# heading

Body text.
"""

EXPECTED = """---
domain: engineering
date-created: 2026-01-01
date-revised: 2026-10-08
status: active
tags:
  - alpha
  - beta
---

# heading

Body text.
"""


def test_removes_all_three_keys_including_list_forms():
    assert strip_provenance(WITH_KEYS) == EXPECTED


def test_unindented_list_items_belong_to_the_key():
    text = "---\nmodels:\n- model-a\n- model-b\ntitle: x\n---\nbody\n"
    assert strip_provenance(text) == "---\ntitle: x\n---\nbody\n"


def test_folded_scalar_continuation_removed():
    text = "---\nsession-link: >\n  https://example.invalid/\n  more\ndate-revised: 2026-10-08\n---\n"
    assert strip_provenance(text) == "---\ndate-revised: 2026-10-08\n---\n"


def test_other_keys_and_date_revised_untouched_byte_for_byte():
    text = "---\r\ntitle:   spaced  \r\nmodels: [a]\r\ndate-revised: 2026-10-08   \r\nnested:\r\n  child: 1\r\n---\r\nbody\r\n"
    out = strip_provenance(text)
    assert out == "---\r\ntitle:   spaced  \r\ndate-revised: 2026-10-08   \r\nnested:\r\n  child: 1\r\n---\r\nbody\r\n"


def test_similar_key_names_are_kept():
    text = "---\nmodels-used: x\nmy-providers: y\nsession-linkage: z\n---\n"
    assert strip_provenance(text) == text


def test_file_without_frontmatter_unchanged():
    text = "# title\n\nmodels: not frontmatter\nproviders: also not\n"
    assert strip_provenance(text) == text


def test_unterminated_frontmatter_unchanged():
    text = "---\nmodels: a\nno closing fence\n"
    assert strip_provenance(text) == text


def test_body_models_line_untouched():
    text = "---\ntitle: x\nmodels: a\n---\n\nmodels: this stays\nproviders: so does this\n---\nsession-link: and this\n"
    assert strip_provenance(text) == "---\ntitle: x\n---\n\nmodels: this stays\nproviders: so does this\n---\nsession-link: and this\n"


def test_idempotent():
    once = strip_provenance(WITH_KEYS)
    assert strip_provenance(once) == once


def test_cli_rewrites_in_place_and_reports(tmp_path: Path, capsys):
    dirty = tmp_path / "dirty.md"
    clean = tmp_path / "clean.md"
    dirty.write_text(WITH_KEYS)
    clean.write_text(EXPECTED)

    assert main([str(dirty), str(clean)]) == 0
    out = capsys.readouterr().out
    assert str(dirty) in out
    assert str(clean) not in out
    assert dirty.read_text() == EXPECTED
    assert clean.read_text() == EXPECTED


def test_cli_check_reports_without_writing(tmp_path: Path, capsys):
    dirty = tmp_path / "dirty.md"
    dirty.write_text(WITH_KEYS)

    assert main(["--check", str(dirty)]) == 1
    assert str(dirty) in capsys.readouterr().out
    assert dirty.read_text() == WITH_KEYS


def test_cli_check_passes_on_clean_files(tmp_path: Path):
    clean = tmp_path / "clean.md"
    clean.write_text(EXPECTED)
    assert main(["--check", str(clean)]) == 0
