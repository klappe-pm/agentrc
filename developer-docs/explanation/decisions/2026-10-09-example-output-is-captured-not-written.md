# 2026-10-09-example-output-is-captured-not-written

## status

accepted

## context

The worked example under [examples/notes-cli](../../../examples/notes-cli/README.md) shows what a sync of a source root produces. Output written by hand looks plausible and goes stale as the engine changes, which makes the example teach the wrong thing. The example also needs a managed project, and the engine finds managed projects by looking in a status directory such as `active` one level under `projects_root`.

## decision

The files under [examples/notes-cli/expected](../../../examples/notes-cli/expected/README.md) are real output of `stratarc sync`, and [tests/test_examples.py](../../../tests/test_examples.py) verifies them byte for byte against a sync of the example source in a temporary home. Anything not captured is labelled illustrative in the example's README. A managed project for the example must sit at `~/projects/active/<name>`, because the engine looks one level under `projects_root` for a status directory.

## alternatives

- Write the expected files by hand. It is quick, but nothing ties them to the engine.
- Compare only file names or structure. It catches missing files but not wrong content.
- Place the example checkout anywhere. It is more convenient, but the engine would not find the project, so the example would not run.
- Regenerate the files without a test. It would keep them current only while someone remembers to do it.

## consequences

- A change to rendering fails the example test until the expected files are recaptured, so every output change is visible in review.
- Machine-specific values, such as the home directory, need a placeholder, and engine-owned files that carry times or absolute paths are listed as excluded in the test.
- The example README can promise only what is captured, and labels the rest, such as the check and diff listings.
- Users following the example must create the project under the `active` directory, which the README states.
