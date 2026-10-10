# ui

This page is the reference for `stratarc ui`, the full-screen terminal interface. It describes the layout, the keys, the monochrome style, the optional extra and the exit statuses. The design is in [cli-design](../../developer-docs/explanation/cli-design.md).

## the-extra

The interface needs Textual, which is not a dependency of the base install. Install it with `pip install "stratarc[ui]"`. Without it `stratarc ui` prints a message that names the extra and exits 5. Importing `stratarc.ui` never loads Textual.

## commands

| command | effect |
| --- | --- |
| `stratarc ui` | open the interface on the active source root |
| `stratarc ui --root PATH` | open the interface on the source root at `PATH` |

## layout

The screen fits 80 columns by 24 rows. On the left is a tree of the source, its projects and each project's agents. The source lists every resolved key. A project or agent lists only the keys its own layers decide. On the right is the pane for the selected node, titled with the current mode and the node. The last row is the key help.

Selecting a value shows its resolved value and the file and line that decided it. Selecting a source, project or agent shows its resolved values.

## keys

| key | effect |
| --- | --- |
| arrow keys, `enter` | move through the tree and expand or collapse a node |
| `e` | open the file that owns the selected node in `$VISUAL` or `$EDITOR` |
| `x` | explain the selected value, or a project's inheritance outline |
| `l` | show the change-log entries for the selected project or key |
| `s` | preview a sync, the same text as `stratarc sync --diff`; nothing is written |
| `v` | run verify, the same report as `stratarc verify run` |
| `q` | cancel a running job and quit |

Every key calls the same library function as its command, so the interface and the command line cannot disagree.

### editing

`e` suspends the screen, runs the editor on the owning file and checks the result with the validator `stratarc config edit` uses. A valid result is kept and the tree is rebuilt from it. An invalid result is listed in the pane and the original is put back through the same safe write the commands use. A copy of the file from before the edit, and of the rejected text, stays in the home's `backups/`. The root's `stratarc.toml` must load, the root's `permissions.json` must satisfy the bundled schema, and any other file must parse.

### slow work

`s` and `v` run in a worker thread. While one runs, the key help row shows a busy line and the other keys keep working: the tree moves and `x` and `l` answer. The result replaces the pane when the job finishes. A second `s` or `v` while a job runs is refused with a note in the help row. `q` cancels the job and quits; the interface does not wait for a result it no longer needs.

## monochrome

When `NO_COLOR` is set to any non-empty value the interface draws without color: borders are white, the cursor is shown in reverse video and the help row is reversed. Nothing is conveyed by color alone.

## exit-statuses

| status | meaning |
| --- | --- |
| 0 | the interface ran and was closed |
| 2 | the source root does not exist or is not a directory (`msg-1001`), or its `stratarc.toml` cannot be used (`msg-1002`) |
| 5 | Textual is not installed (`msg-1153`), or input or output is not a terminal (`msg-1160`) |
| 130 | interrupted |

The checks run before the screen opens, in this order: the source root, then the extra, then the terminal. A missing root is therefore reported even when Textual is also missing. The interface needs a terminal; with its input or output redirected the command prints `msg-1160` and exits 5 instead of waiting for input. `stratarc ui --help` runs none of the checks.
