#!/usr/bin/env bash
# Behavioral tests for hooks/lib/prose-detect.py
#
# The shared detector behind the no-emdash / no-hardwrap PreToolUse guards and
# the git pre-commit backstop. Feeds text on stdin and asserts the printed
# reason (empty means clean). The forbidden glyphs are built from bytes so this
# file carries no literal em-dash for the live guard to block on write.

set -euo pipefail

DETECT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../stratarc/data/hooks/lib" && pwd)/prose-detect.py"

EMDASH=$'\xe2\x80\x94'   # U+2014
HBAR=$'\xe2\x80\x95'     # U+2015
ENDASH=$'\xe2\x80\x93'   # U+2013

PASS=0
FAIL=0

# assert_detect <label> <mode> <text> <expected-reason>
assert_detect() {
  local label="$1" mode="$2" text="$3" expect="$4"
  local got
  got="$(printf '%s' "$text" | python3 "$DETECT" "$mode" 2>/dev/null || true)"
  if [ "$got" = "$expect" ]; then
    PASS=$((PASS + 1))
  else
    FAIL=$((FAIL + 1))
    printf '  prose-detect.py FAIL  %-40s expected=%q got=%q\n' "$label" "$expect" "$got" >&2
  fi
}

# em-dash mode
assert_detect "em-dash caught"            emdash "a ${EMDASH} b"      "em-dash"
assert_detect "horizontal bar caught"     emdash "a ${HBAR} b"        "em-dash"
assert_detect "connector en-dash caught"  emdash "foo ${ENDASH} bar"  "en-dash-connector"
assert_detect "numeric-range en-dash ok"  emdash "pages 2${ENDASH}3"  ""
assert_detect "clean prose ok"            emdash "clean: really"      ""
assert_detect "empty text ok"             emdash ""                   ""
# Code is not prose. the no-em-dash rule ends with "Quote source text that
# contains dashes verbatim only inside a code fence", an exemption the detector
# never implemented: it tested the raw string, so a dash inside a fence or a
# backtick span was flagged exactly like one in a sentence, and ALLOW_EMDASH, a
# bypass needing approval, was the only way to record a literal. A product
# watermark string in an AGENTS.md hit this.
assert_detect "inline code exempt"        emdash "the \`A ${EMDASH} B\` watermark"        ""
assert_detect "fenced block exempt"       emdash $'t\n```\nq '"${EMDASH}"$' s\n```\nmore'  ""
assert_detect "prose after fence caught"  emdash $'```\nq '"${EMDASH}"$' s\n```\n\nreal '"${EMDASH}"$' dash' "em-dash"
assert_detect "prose beside code caught"  emdash "\`ok\` and a ${EMDASH} dash"            "em-dash"
assert_detect "en-dash in code exempt"    emdash "see \`foo ${ENDASH} bar\` here"         ""

# Issue #289: frontmatter is metadata, not prose, exactly as detect_hardwrap
# already treats it. A dashed title quoted into a frontmatter field (the
# shape hooks/lib/prompt-capture.py's aliases: line produces) must not trip
# the rule, but a dash in the body after the closing fence still must.
assert_detect "dash only in frontmatter ok" emdash $'---\naliases:\n  - "a '"${EMDASH}"$' b"\nstatus: DRAFT\n---\n\nclean body text' ""
assert_detect "dash in body after frontmatter caught" emdash $'---\nstatus: DRAFT\n---\n\nreal '"${EMDASH}"$' dash' "em-dash"
assert_detect "unterminated frontmatter has nothing left to scan" emdash $'---\naliases:\n  - "a '"${EMDASH}"$' b"' ""

# hard-wrap mode
assert_detect "wrapped paragraph caught"  hardwrap $'para line one\npara line two'  "para line two"
assert_detect "single-line paragraph ok"  hardwrap "One clean line."                ""
assert_detect "separate list items ok"    hardwrap $'- item one\n- item two'        ""
assert_detect "wrapped list item caught"  hardwrap $'- item one\n  continues here'  "continues here"
assert_detect "blank-separated paras ok"  hardwrap $'para one.\n\npara two.'         ""
assert_detect "table rows ok"             hardwrap $'| A | B |\n|---|---|\n| 1 | 2 |' ""
assert_detect "nested fence example ok"   hardwrap $'````markdown\n```mermaid\nA[Start] --> B[Done]\n```\n````' ""

# YAML frontmatter fragments. An Edit sends only its new_string, so a
# frontmatter edit arrives with no surrounding --- fence; its adjacent
# "key: value" lines must not read as a wrapped paragraph.
assert_detect "frontmatter fragment ok"   hardwrap $'approved-by: example-org\nstatus: ACCEPTED' ""
assert_detect "frontmatter seq items ok"  hardwrap $'tags:\n  - alpha\n  - beta'                ""
assert_detect "single key line ok"        hardwrap "status: ACCEPTED"                           ""
assert_detect "nested frontmatter keys ok" hardwrap $'metadata:\n  type: lesson\n  recurrence: 3\n  enforced-by: "x"\nname: foo' ""
# The all-lines-match test must not blind the detector: a fragment that mixes a
# key with wrapped prose is still a violation.
assert_detect "key plus wrapped prose caught" hardwrap $'status: ACCEPTED\nthis paragraph wraps\nonto another line' "this paragraph wraps"
assert_detect "mid-sentence colon still caught" hardwrap $'The ratio is 3:1 and it wraps\nonto a second line.' "onto a second line."

# Obsidian Templater blocks. A Templater template is a *.md note whose
# <% ... %> blocks hold JavaScript, and ordinary source is nothing but adjacent
# non-blank lines, so without the exemption no Templater script can be authored
# at all. Contents are skipped exactly like a fenced code block.
assert_detect "templater block skipped"   hardwrap $'<%*\nconst a = 1;\nconst b = 2;\n%>'        ""
assert_detect "inline templater ok"       hardwrap $'<% tp.date.now() %>\n\nOne clean line.'     ""
assert_detect "prose before templater ok" hardwrap $'One clean line.\n\n<%*\nconst a = 1;\nconst b = 2;\n%>' ""
# The exemption must not blind the detector: prose after a closed block is still checked.
assert_detect "wrap after templater caught" hardwrap $'<%*\nconst a = 1;\n%>\n\nthis paragraph wraps\nonto another line' "onto another line"

# Display math. A `$$` block holds mathematics, not prose, so its body is never
# a wrapped paragraph. Without the math state the equation row of a multi-line
# block reads as a lazy continuation of the `$$` above it, and the guard denies
# a write that follows the note schema's own documented example. Three authoring
# agents hit this independently on 2026-09-15 before it was fixed.
assert_detect "multi-line display math skipped" hardwrap $'$$\n\\kappa = \\frac{p_o - p_e}{1 - p_e}\n$$' ""
assert_detect "multi-row equation body skipped" hardwrap $'$$\na = b \\\\\nc = d\n$$'                      ""
assert_detect "single-line display math ok"     hardwrap $'$$ n = 2\\sigma^2 / \\delta^2 $$'               ""
assert_detect "prose before math ok"            hardwrap $'One clean line.\n\n$$\nx = y\n$$'               ""
# The exemption must not blind the detector: prose after a closed block is still checked.
assert_detect "wrap after math caught"          hardwrap $'$$\nx = y\n$$\n\nthis paragraph wraps\nonto another line' "onto another line"
assert_detect "unclosed math does not swallow"  hardwrap $'text one\n\n$$ a = b $$\n\nthis paragraph wraps\nonto another line' "onto another line"

# the no-hardwrapped-writing rule permits an intentional line break, two trailing spaces or a
# trailing backslash, as structure. The scan did not implement that and rejected the one break the
# rule allows, which blocked committing documents whose metadata lines legitimately use it.
assert_detect "two-space break is structure"     hardwrap $'**Date:** 2026-09-20  \n**Scope:** the whole system  \n**Analyst:** someone' ""
assert_detect "backslash break is structure"     hardwrap $'first line\\\nsecond line'                       ""
assert_detect "break inside a list item ok"      hardwrap $'- first half  \n- second item'                   ""
# The exemption must not blind the detector: a line with no break still continues a paragraph.
assert_detect "break does not blind the scan"    hardwrap $'ends with a break  \nthis paragraph wraps\nonto another line' "onto another line"
assert_detect "one trailing space is not a break" hardwrap $'ends with one space \nonto another line'         "onto another line"

# Context-aware scanning. An Edit's new_string is a fragment; when the fence
# opener lies before the edit, the fragment alone reads as wrapped prose. The
# payload mode reads the target file and carries fence state across the edit.
# assert_payload <label> <payload-json> <expected-reason>
assert_payload() {
  local label="$1" payload="$2" expect="$3"
  local got
  got="$(printf '%s' "$payload" | python3 "$DETECT" hardwrap-payload 2>/dev/null || true)"
  if [ "$got" = "$expect" ]; then
    PASS=$((PASS + 1))
  else
    FAIL=$((FAIL + 1))
    printf '  prose-detect.py FAIL  %-40s expected=%q got=%q\n' "$label" "$expect" "$got" >&2
  fi
}
CTX_DIR="$(mktemp -d "${TMPDIR:-/tmp}/prose-detect-ctx-XXXXXX")"
trap 'rm -rf "$CTX_DIR"' EXIT
printf '# doc\n\n```bash\ncmd one\nMARKER\n```\n\nAfter.\n' > "$CTX_DIR/open-fence.md"
printf '# doc\n\nMARKER\n' > "$CTX_DIR/no-fence.md"
mk_payload() {
  python3 -c 'import json,sys; print(json.dumps({"tool_input":{"file_path":sys.argv[1],"old_string":sys.argv[2],"new_string":sys.argv[3]}}))' "$@"
}
assert_payload "fragment inside open fence ok" \
  "$(mk_payload "$CTX_DIR/open-fence.md" MARKER $'cmd two\ncmd three')" ""
assert_payload "same fragment with no fence caught" \
  "$(mk_payload "$CTX_DIR/no-fence.md" MARKER $'cmd two\ncmd three')" "cmd three"
assert_payload "prose after closing fence still caught" \
  "$(mk_payload "$CTX_DIR/open-fence.md" MARKER $'cmd two\n```\n\nwrapped prose\nsecond line')" "second line"
assert_payload "wrap across the join is not the author's" \
  "$(mk_payload "$CTX_DIR/no-fence.md" MARKER 'one clean line')" ""
assert_payload "old_string absent scans fragment alone" \
  "$(mk_payload "$CTX_DIR/open-fence.md" ABSENT $'cmd two\ncmd three')" "cmd three"
assert_payload "missing file scans fragment alone" \
  "$(mk_payload "$CTX_DIR/nope.md" MARKER $'para one\npara two')" "para two"
assert_payload "write content scanned whole" \
  "$(python3 -c 'import json; print(json.dumps({"tool_input":{"file_path":"/tmp/x.md","content":"para one\npara two"}}))')" "para two"
assert_payload "malformed payload is clean" 'not json' ""

# Review cluster F2: the handoff format the session-handoff-on-pr rule mandates
# puts one bold field label per line. Each such line is its own field, not a
# wrapped paragraph; a plain line after a field line is still a wrap.
HANDOFF=$'## session handoff\n\n**Delivered:** commit abc adds the gate.\n**Gates:** python3 scripts/test.py passed.\n**Next:** merge.\n**Blocked:** none\n**Do not:** none\n**Open decisions:** none'
assert_detect "handoff field lines clean"      hardwrap "$HANDOFF" ""
assert_detect "plain line after field wraps"   hardwrap $'**Gates:** first half of a sentence\nthat was wrapped here' "that was wrapped here"
assert_detect "field line opens a new line"  hardwrap $'a paragraph line\n**Note:** starts a field' ""
assert_detect "bold word mid-sentence wraps"   hardwrap $'**Bold** opening words\nthen a wrapped line' "then a wrapped line"

# Review cluster F2: non-UTF-8 Markdown is judged, never a crash. The staged
# prose gate read the traceback as a clean file.
latin_wrap="$(printf 'caf\xe9 line one\nline two wraps\n' | python3 "$DETECT" hardwrap 2>&1 || true)"
[ "$latin_wrap" = "line two wraps" ] && PASS=$((PASS + 1)) \
  || { FAIL=$((FAIL + 1)); printf '  prose-detect.py FAIL  hardwrap on non-UTF-8 text: got %q\n' "$latin_wrap" >&2; }
latin_clean="$(printf 'caf\xe9 one clean line\n' | python3 "$DETECT" emdash 2>&1 || true)"
[ -z "$latin_clean" ] && PASS=$((PASS + 1)) \
  || { FAIL=$((FAIL + 1)); printf '  prose-detect.py FAIL  emdash on clean non-UTF-8 text: got %q\n' "$latin_clean" >&2; }
latin_dash="$(printf 'caf\xe9 a %s b\n' "$EMDASH" | python3 "$DETECT" emdash 2>&1 || true)"
[ "$latin_dash" = "em-dash" ] && PASS=$((PASS + 1)) \
  || { FAIL=$((FAIL + 1)); printf '  prose-detect.py FAIL  emdash beside a non-UTF-8 byte: got %q\n' "$latin_dash" >&2; }

# the no-redundant-labels rule: a table cell never opens with a label that
# repeats its column header, and a list item never opens with its heading's
# noun as a label. The reason is the offending line, as for hard wrap.
assert_detect "cell label equal to header caught"   labels $'| gap | owner |\n|---|---|\n| Gap: no SQL analysis | Kevin |' "| Gap: no SQL analysis | Kevin |"
assert_detect "cell label prefix of header caught"  labels $'| Gaps found | owner |\n|---|---|\n| x | y |\n| GAP: thin record | z |' "| GAP: thin record | z |"
assert_detect "bold cell label caught"              labels $'| gap |\n|---|\n| **Gap:** thin |' "| **Gap:** thin |"
assert_detect "plain cell ok"                       labels $'| gap | owner |\n|---|---|\n| no SQL analysis | Kevin |' ""
assert_detect "other cell label ok"                 labels $'| gap | owner |\n|---|---|\n| Note: thin | Kevin |' ""
assert_detect "time in a cell is not a label"       labels $'| time |\n|---|\n| 10:30 start |' ""
assert_detect "header row itself ok"                labels $'| Gap: header |\n|---|\n| x |' ""
assert_detect "item opening with heading noun caught" labels $'## gaps\n\n- Gap: no SQL analysis' "- Gap: no SQL analysis"
assert_detect "item opening with heading phrase caught" labels $'## next-actions\n\n- Next action: Kevin sends it' "- Next action: Kevin sends it"
# A header or heading joining several things with and, or, vs names alternatives,
# and a label then says which one applies, so it is not redundant.
assert_detect "label choosing among heading nouns ok"  labels $'## inputs-and-outputs\n\n- Input: the config\n- Output: one line' ""
# The heading's noun is its last word or words, not any word in it.
assert_detect "label matching a mid-heading word ok"   labels $'### per-user-memory\n\n- user: who the developer is' ""
# A list whose items carry different labels is a record of fields; the label
# that echoes the heading is one field name among several, not redundancy.
assert_detect "field record list ok"                   labels $'### successful-patterns\n\n- **Pattern**: central config\n- **Evidence**: 21 projects\n- **Impact**: easy updates' ""
assert_detect "repeated heading label in a plain list caught" labels $'## gaps\n\n- thin record\n- Gap: no SQL' "- Gap: no SQL"
# The H1 is the file title, not a section heading.
assert_detect "file title does not govern items"       labels $'# fix-documents\n\n1. Find: reproduce it\n2. Document: record it' ""
assert_detect "label choosing among header nouns ok"   labels $'| evidence or gap |\n|---|\n| Evidence: shipped the gate |' ""
assert_detect "numbered item caught"                labels $'## sources\n\n1. Source: the 10-K' "1. Source: the 10-K"
assert_detect "item with another label ok"          labels $'## gaps\n\n- Owner: Kevin' ""
assert_detect "item without a colon ok"             labels $'## gaps\n\n- Gap analysis is still open' ""
assert_detect "handoff field labels ok"             labels $'## session handoff\n\n- Delivered: commit abc\n- Next: merge' ""
assert_detect "item before any heading ok"          labels $'- Gap: no heading above' ""
assert_detect "nearest heading governs"             labels $'## gaps\n\n### owners\n\n- Gap: under owners' ""
assert_detect "fenced example not scanned"          labels $'## gaps\n\n```\n- Gap: example\n```' ""
assert_detect "frontmatter not scanned"             labels $'---\ntags: [gap]\n---\n\n## gaps\n\n- thin record' ""

# Payload mode reads the target file, so an Edit fragment sees the heading or
# header above it; a violation already in the file is not the author's.
printf '# doc\n\n## gaps\n\nMARKER\n' > "$CTX_DIR/gaps.md"
printf '# doc\n\n## gaps\n\n- Gap: old line\nMARKER\n' > "$CTX_DIR/gaps-old.md"
printf '# doc\n\n| gap | owner |\n|---|---|\nMARKER\n' > "$CTX_DIR/table.md"
mk_labels_payload() {
  python3 -c 'import json,sys; print(json.dumps({"tool_input":{"file_path":sys.argv[1],"old_string":sys.argv[2],"new_string":sys.argv[3]}}))' "$@"
}
assert_labels_payload() {
  local label="$1" payload="$2" expect="$3"
  local got
  got="$(printf '%s' "$payload" | python3 "$DETECT" labels-payload 2>/dev/null || true)"
  if [ "$got" = "$expect" ]; then
    PASS=$((PASS + 1))
  else
    FAIL=$((FAIL + 1))
    printf '  prose-detect.py FAIL  %-40s expected=%q got=%q\n' "$label" "$expect" "$got" >&2
  fi
}
assert_labels_payload "edit item under heading in file caught" \
  "$(mk_labels_payload "$CTX_DIR/gaps.md" MARKER '- Gap: new line')" "- Gap: new line"
assert_labels_payload "edit row under header in file caught" \
  "$(mk_labels_payload "$CTX_DIR/table.md" MARKER '| Gap: thin | Kevin |')" "| Gap: thin | Kevin |"
assert_labels_payload "existing violation is not the author's" \
  "$(mk_labels_payload "$CTX_DIR/gaps-old.md" MARKER '- a clean new line')" ""
assert_labels_payload "write content scanned whole" \
  "$(python3 -c 'import json; print(json.dumps({"tool_input":{"file_path":"/tmp/x.md","content":"## gaps\n\n- Gap: x"}}))')" "- Gap: x"
assert_labels_payload "malformed payload is clean" 'not json' ""

# usage error exits nonzero without printing a reason
if python3 "$DETECT" bogus </dev/null >/dev/null 2>&1; then
  FAIL=$((FAIL + 1)); printf '  prose-detect.py FAIL  bad mode should exit nonzero\n' >&2
else
  PASS=$((PASS + 1))
fi

printf 'prose-detect.py: %d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
