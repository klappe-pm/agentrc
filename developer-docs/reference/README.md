# reference

Reference pages describe the codebase as it is: the layout of a source root, how packaged assets are found, the shape of each file. They are for looking something up while you change the code, so they are organised around the thing described, state facts without instruction, and aim to be complete. The how-to guides say what to do with these facts.

## pages

- [source layout](source-layout.md): the source root that `stratarc init` creates, what each file and directory holds and which runtimes consume it.
- [package data](package-data.md): how the templates, schemas, hooks and git hooks under `stratarc/data/` are packaged and resolved through `importlib.resources`.
