# 2026-10-09-projects-root-from-config

## status

Accepted, 2026-10-09, by the maintainer.

## context

The engine delivers configuration into project checkouts and walks status directories (such as `active/` and `archived/`) to find projects. The location of those checkouts was settable only by a flag or an environment variable, and three modules each carried their own copy of the rule that maps the default checkout directory to the directory that holds the status directories.

## decision

`projects_root` in `stratarc.toml` feeds `paths.projects_root()`, and the precedence is the command line flag, then the environment variable, then `stratarc.toml`, then the default `<home>/projects/active`. `paths.projects_dir()` is the single place that maps the default checkout directory to its parent, `<home>/projects`, for status directory walks; a value from the flag, the environment or `stratarc.toml` names the directory itself and is used as given. This replaces three copies of that mapping. Both functions are in [paths.py](../../../stratarc/paths.py), and the config key is read by [config.py](../../../stratarc/config.py).

## alternatives

- Keep the flag and the environment variable only. Rejected: a source root could not state where its projects live, so every user and every script had to repeat it.
- Always treat the configured value as a checkout directory and derive the parent. Rejected: a user who sets the key to the directory that holds status directories would get the wrong parent.
- Leave the three copies in place. Rejected: one function is a single place to test and to change.

## consequences

- A source root carries its own projects location, so the same command works on any machine that shares it.
- An explicit flag still overrides everything, which keeps tests and one-off runs simple.
- The two functions take their arguments in different orders, which the docstring of `projects_dir` warns about.
