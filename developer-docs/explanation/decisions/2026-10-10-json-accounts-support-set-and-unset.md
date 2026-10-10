# 2026-10-10-json-accounts-support-set-and-unset

## status

accepted, 2026-10-10

## context

`account edit NAME --set KEY=VALUE --unset KEY` changed only `.toml` account files, through a line editor that keeps comments and layout. An account kept as `.json` exited 2 with `invalid-edit` and had to go through the editor, although the account resource accepts both formats everywhere else (`list`, `show`, the layers).

## decision

`--set` and `--unset` work on a `.json` account file with the same semantics as TOML. The key is a dotted path, `--set` creates missing objects along it, `--unset` of a key that is not set exits 2 with `unknown-key`, setting and unsetting one key is refused, and a value is read as JSON first and as a string otherwise. The file is parsed, changed in memory and written back with two-space indentation and a final newline. Existing keys keep their order and a new key goes last. The result is validated and saved through the shared save, so the backup, the newer-schema refusal and `--dry-run` behave as for TOML. A file that does not parse, or whose top level is not an object, exits 2 with `invalid-edit` and is left as it was.

## alternatives

Keeping the refusal was rejected because it made the two formats unequal for no gain. A line editor for JSON was rejected because JSON has no comments to preserve, so a parse and a dump lose nothing but the author's indentation. Preserving the original indentation was rejected as more code for a cosmetic gain.

## consequences

A hand-formatted JSON account is reindented to two spaces on its first scripted edit, which shows once in a diff. The editor path is no longer the only way to change a JSON account.
