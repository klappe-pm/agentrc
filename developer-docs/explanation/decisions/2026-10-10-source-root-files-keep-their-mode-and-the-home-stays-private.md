# 2026-10-10-source-root-files-keep-their-mode-and-the-home-stays-private

## status

accepted, 2026-10-10

## context

`home_layout.safe_write` wrote every file with mode 0600, which suits the home (`~/.stratarc`) and is wrong for a source root. A source root is a user-visible directory that is often committed, shared with a team and read by other tools. A write verb such as `account edit --set` silently turned a 0644 file into 0600, and a 0755 script into a non-executable one, while `source init` wrote its files with the default 0644. The same tool therefore gave a source root two different modes depending on which verb touched it.

## decision

`safe_write` takes a `private` parameter that defaults to true. A private write keeps today's behavior: files 0600 and a missing parent directory 0700, with the missing ancestors inside the home made 0700 as well. A write with `private=False` is for a source root. The file keeps the mode of the file it replaces, a new file is 0644 and a missing parent directory is 0755, both subject to the umask, which matches `source init`. An existing directory is never changed. The backup copy is 0600 in both cases because it lives in the home. The source-root callers in `stratarc/resources_cmd.py` pass `private=False`: the shared save used by every resource editor and the control-plane write. Every other caller keeps the default.

## alternatives

Making every write 0644 was rejected because the home holds provider references and state that should not be readable by other users. Reading a mode from the project configuration was rejected as more surface than the problem needs. Splitting the function in two was rejected because the backup, schema and atomic-replace logic is the same and would be duplicated. Changing `source init` to 0600 was rejected because a scaffolded tree is meant to be committed and shared.

## consequences

A source-root file is no longer changed in mode by an edit, so a committed file shows no mode change in a diff. The umask decides new files, as for any other tool that creates files. While writing the tests a second defect surfaced: when the first write into a fresh home was a backup, `.stratarc` itself was created with the default mode 0755 because only the leaf directory was changed to 0700. The ancestors inside the home are now changed too, and a test covers it.
