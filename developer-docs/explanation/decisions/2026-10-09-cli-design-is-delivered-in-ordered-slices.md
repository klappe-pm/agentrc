# 2026-10-09-cli-design-is-delivered-in-ordered-slices

## status

accepted

## context

[cli-design](../cli-design.md) describes a much larger command line than the one that exists. Building it in one change would be unreviewable, and building it ad hoc would leave readers unable to tell what is real. Several parts depend on others: the editors need the resolver, the interface needs the writers, and the documentation needs the commands it describes.

## decision

The full design in `cli-design.md` ships in the order recorded in [cli-backlog](../cli-backlog.md): config, the source, project, runtime and agent resources, provider and adapter, home, change log, verify, log, api, ui and docs. Each slice names its prerequisite, so a contributor can pick the next one without reading the whole design. What is not built is listed in the backlog under its order, known defects and open questions, not left implicit.

## alternatives

- Deliver the design as one large change. It avoids intermediate states, but nothing could be reviewed or released until all of it was done.
- Keep the design as a wish list with no order. It is cheap, but contributors cannot tell what is blocked and the shipped surface becomes unclear.
- Order by user value alone. It would ship visible features first, but would build the interface and docs on resolvers and writers that did not yet exist.

## consequences

- The design page stays a design and the backlog is the record of what has shipped and what remains.
- A slice that is built moves from the order list to the shipped list in the same change, so the two pages agree.
- A reordering is a deliberate edit to the backlog and, when it changes a prerequisite, a new decision record.
- Features can exist in the design for a long time before they exist in the code, and readers must check the backlog before relying on one.
