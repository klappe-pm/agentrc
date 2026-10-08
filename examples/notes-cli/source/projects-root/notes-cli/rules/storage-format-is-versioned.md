# storage-format-is-versioned

## binding

Every change to the on-disk note format bumps `FORMAT_VERSION` in `notes_cli/storage.py` and adds a migration under `notes_cli/migrations/`. A change that alters the format without both is incomplete.

## rationale

People keep years of notes in this format. A silent change strands them on a version the tool can no longer read, so the version number and the migration are what let an old directory open in a new release.
