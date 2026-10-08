# decisions

This folder holds agentrc's decision records: one page per choice that shaped the code, written when the choice was made, so a later contributor can see what was decided, what was considered instead and why. A record is never rewritten once accepted; a reversal is a new record that names the one it supersedes. Read the records that touch a part of the code before proposing to change it.

## format

Each record is a file named `YYYY-MM-DD-<short-kebab-title>.md` with these sections: `## context` (the situation and the forces at play), `## decision` (what was chosen, in one paragraph), `## alternatives` (what else was considered and why it lost), and `## consequences` (what follows, good and bad). Add the record to the list below when it is accepted.

## records

None yet. The first records will cover the choices already visible in the code: keeping runtime assets under `agentrc/data/` and reading them through `importlib.resources`, shipping the attribution and token-shaped value detectors with the package, and placing the user and developer documentation in two trees with the same quadrant shape.
