# 2026-10-09-private-checks-gate-deploy-only-when-the-source-root-opts-in

## status

accepted, 2026-10-09. Settles the open point in [private extensions live in the source root](2026-10-09-private-extensions-live-in-the-source-root.md).

## context

A source root can load its own validator checks from `scripts/private/validate_checks.py`. The deploy gate runs the generic checks only, so a source root's own checks run under `stratarc validate` and never block a sync. A maintainer who relies on private checks to keep a deploy safe needs a way to make them block.

## decision

The deploy gate runs the source root's private checks when `stratarc.toml` sets `[validate] gate_private = true`. The default is false, so a public user's deploys depend only on the generic checks the package ships. A private check that raises or fails to load is an error finding, and under the gate it blocks the deploy with a message naming the file.

## alternatives

Always gating on private checks was rejected because a user with a stale or broken extension would find every sync blocked for a reason outside the package. Never gating was rejected because it leaves the maintainer's own safety checks advisory. A command-line flag only was rejected because the choice belongs to the source root and should not depend on who runs the sync.

## consequences

The maintainer can turn private checks into a gate with one line and keep them off for everyone else. The setting is part of `stratarc.toml`, so it is reviewed with the source it protects.
