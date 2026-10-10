# 2026-10-10-shellcheck-gates-at-warning-severity

## status

accepted, 2026-10-10

## context

The shell lint job in CI ran shellcheck at its default severity over every tracked shell script, including the guard scripts moved into package data and their tests. It was never run locally during the move, and on the first pull request it failed with 470 findings. Four were warnings: one unsplit declare and assign, and three suspect quote closings in a test that splices string literals so the file does not trip the detector it tests. The other 466 were style notes, 411 of them the `[ test ] && ok || bad` idiom that the test harnesses use on purpose, and the rest were notes about single-quoted expressions and sourced files shellcheck cannot follow.

## decision

CI runs shellcheck with `-S warning`, so a warning or an error fails the job and a style note does not. The four warnings were fixed in the same change: the declare and assign was split, and the spliced literals in the attribution test moved into named variables. A contributor who wants the notes can run shellcheck without the flag.

## alternatives

Fixing every note was rejected because 411 of them would mean rewriting a deliberate test idiom into longer code with no change in behavior, and the hooks have 548 and 60 assertion tests that already pin their behavior. Disabling the lint job was rejected because the warnings it found were real. Adding a long list of per-code directives to the scripts was rejected because it spreads the same decision across hundreds of lines.

## consequences

New warnings and errors in any tracked shell file fail CI, and the job stays quiet about idioms the maintainers chose. If the notes become worth fixing, lowering the gate back to the default is one flag and does not need a new record beyond a note that supersedes this one.
