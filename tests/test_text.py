"""strip_jsonc_comments and set_top_level, the two text editors shared by the adapters."""

from __future__ import annotations

import json
import tomllib

import pytest

from agentrc.adapters._text import set_top_level, strip_jsonc_comments


class TestStripJsoncComments:
    def test_removes_line_and_block_comments(self):
        text = '{\n  // note\n  "a": 1, /* inline */\n  /* multi\n  line */ "b": 2\n}\n'
        assert json.loads(strip_jsonc_comments(text)) == {"a": 1, "b": 2}

    def test_block_comment_keeps_line_numbers(self):
        text = '{\n/* one\ntwo */\n"a": 1}'
        assert strip_jsonc_comments(text).count("\n") == text.count("\n")

    def test_removes_trailing_commas(self):
        assert json.loads(strip_jsonc_comments('{"a": [1, 2,], "b": {"c": 3,},}')) == {"a": [1, 2], "b": {"c": 3}}

    def test_comment_markers_inside_strings_survive(self):
        text = '{"url": "https://example.com/a", "glob": "/* not a comment */"}'
        assert json.loads(strip_jsonc_comments(text)) == json.loads(text)

    def test_comma_shaped_text_inside_a_string_survives(self):
        text = '{"keep": "keep,}", "also": "a, ]"}'
        assert json.loads(strip_jsonc_comments(text)) == {"keep": "keep,}", "also": "a, ]"}

    def test_escaped_quote_does_not_end_the_string(self):
        text = '{"q": "say \\"// hi\\" now"}'
        assert json.loads(strip_jsonc_comments(text)) == {"q": 'say "// hi" now'}

    def test_plain_json_is_unchanged(self):
        text = '{"a": 1}'
        assert strip_jsonc_comments(text) == text


class TestSetTopLevel:
    def test_replaces_an_existing_scalar(self):
        assert set_top_level('model = "a"\nother = 1\n', "model", '"b"') == 'model = "b"\nother = 1\n'

    def test_appends_a_missing_scalar_before_the_first_table(self):
        result = set_top_level('x = 1\n[t]\ny = 2\n', "model", '"b"')
        assert result == 'x = 1\nmodel = "b"\n[t]\ny = 2\n'

    def test_appends_when_the_head_lacks_a_trailing_newline(self):
        assert set_top_level("x = 1", "model", '"b"') == 'x = 1\nmodel = "b"\n'

    def test_none_removes_the_scalar(self):
        assert set_top_level('model = "a"\nother = 1\n', "model", None) == "other = 1\n"

    def test_removing_an_absent_key_changes_nothing(self):
        text = "other = 1\n"
        assert set_top_level(text, "model", None) == text

    def test_a_key_inside_a_table_is_never_touched(self):
        text = 'a = 1\n[t]\nmodel = "inner"\n'
        result = set_top_level(text, "model", '"outer"')
        assert tomllib.loads(result) == {"a": 1, "model": "outer", "t": {"model": "inner"}}
        assert set_top_level(text, "model", None) == text

    def test_replaces_a_multiline_array_whole(self):
        text = 'include_only = [\n  "ONE",\n  "TWO",\n]\nnext = 1\n'
        updated = set_top_level(text, "include_only", json.dumps(["X"]))
        assert tomllib.loads(updated) == {"include_only": ["X"], "next": 1}
        assert set_top_level(text, "include_only", None) == "next = 1\n"

    @pytest.mark.parametrize("bracket_value", ['"a ] b"', "'c [ d'", '"""x\n]\n"""'])
    def test_brackets_inside_strings_do_not_end_the_value(self, bracket_value):
        text = f"key = {bracket_value}\nafter = 1\n"
        assert tomllib.loads(set_top_level(text, "key", '"new"')) == {"key": "new", "after": 1}

    def test_a_comment_after_the_value_goes_with_it(self):
        assert set_top_level('key = "a" # why\nz = 1\n', "key", '"b"') == 'key = "b"\nz = 1\n'
