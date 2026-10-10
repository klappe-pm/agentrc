# 2026-10-09-rendered-workflows-must-run-files-the-sync-delivers

## status

Accepted, 2026-10-09, by the maintainer.

## context

The attribution check workflows are rendered into each managed project, and the script they run is delivered to `.github/attribution-check/attribution-check.py`. The packaged templates named the script as `scripts/ci/attribution-check.py` and the renderer replaced the bare file name with the delivered path. That replacement turned the path into `scripts/ci/.github/attribution-check/attribution-check.py`, which exists in no project, so the check failed on every pull request and push of every project it was rendered into. The earlier tests only looked for the file name in the workflow text, so they passed.

## decision

The packaged workflows under `stratarc/data/ci/` name the script at its delivered project path, `.github/attribution-check/attribution-check.py`, and the renderer copies them byte for byte instead of rewriting the name. A permanent test renders a project into a temporary checkout and asserts that every `python3 <path>` in each rendered workflow resolves to a file the same sync delivered. A second test asserts the same for this repository's own workflows against the files it holds.

## alternatives

- Keep the replacement and fix the pattern it matches. Rejected: a text rewrite of a template is the failure that caused this, and a template that is already correct needs no rewrite.
- Deliver the script to `scripts/ci/` as well. Rejected: it would add a second copy to every project to satisfy a path nobody needs.

## consequences

- The rendered check runs in a project as delivered, and drift verification compares the packaged bytes directly.
- A future template that names a script the sync does not deliver fails the resolution test before it ships.
