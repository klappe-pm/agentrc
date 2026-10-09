# 2026-10-09-no-deploy-branch-guard-unless-environments-declared

## status

Accepted, 2026-10-09, by the maintainer.

## context

A deploy writes whatever the checkout it runs in holds, so the engine refuses to deploy from a branch that is not the one an environment promotes. That guard was built for a workstation with several checkouts. A plain source root has none of that: it may sit on any branch, or not be a git checkout at all, and refusing it would make the first run fail for no reason the user can see.

## decision

`deploy_ref` in [deploy_guard.py](../../../stratarc/deploy_guard.py) returns None, meaning no branch guard, when no environment is selected and components.json declares none, so a plain source root deploys from any branch or from no git at all. A source root that declares environments keeps the guard. An environment is selected by `STRATARC_ENVIRONMENT`, then by the checkout's `<name>.environment` git config key, where the name is the engine name. When environments are declared but none is selected, the previous behavior is kept and the checkout must be on `main`; this was a judgment call, taken because a source root that declared environments has opted into the guard and a silent skip would defeat it. A selected environment that components.json does not declare is an error. The stamp and deploy record file names derive from the engine name.

## alternatives

- Always require `main`. Rejected: it blocks every plain source root that works on another branch or outside git.
- Never guard the branch. Rejected: it removes the protection from the source roots that depend on it.
- Skip the guard when environments are declared but none is selected. Rejected as the weaker reading of the opt-in, though the choice is not obvious and a later record may change it.

## consequences

- A new user deploys with no setup and no branch error.
- Declaring `environments` in components.json is the single switch that turns the guard on.
- The default of `main` for declared environments with none selected is a judgment call that this record flags for review.
- The stamp and record names follow the engine name, so two engines with different names keep separate records.
