# feature-documentation-template

This is the outline for a page that documents one feature end to end: a subcommand, a source kind, a hook, an adapter. Copy the sections below into a new file, keep their order, and delete a section only when it is genuinely empty, saying so in one line rather than leaving a gap. The template exists so that two feature pages written by two people read the same way, and so a reader who has used one knows where to look in the next.

Each section below states what belongs in it. Replace the guidance with content; the headings stay as they are.

## title

The feature's name as its H1, lowercase kebab-case, equal to the filename stem. Follow it with one paragraph: what the feature does, who uses it, and what the page covers. A reader who stops after this paragraph should know whether the page is the one they need.

## table-of-contents

A list linking to each section on the page. Include it when the page runs past a screen or two; a short page says `Not needed for a page this length.` here and moves on.

## terminology

The terms the page relies on, each defined in one line, in alphabetical order. Define a term here even when it is defined elsewhere in the documentation, and link to the fuller definition. A reader should not have to leave the page to parse it.

## details

The body. Describe what the feature does, the inputs it reads, the outputs it writes, its options and defaults, and its behavior at the edges: empty input, a missing file, a conflicting setting. Use sub-headings for each concern and a table for anything with more than three rows. Give every example a code fence with a language tag and show its output where the output matters.

## troubleshooting

Known failures, one sub-heading per failure, each named by the message or symptom a reader actually sees. Under each: the cause and the fix. Link to the shared [troubleshooting](../how-to-guides/troubleshooting.md) page for failures that are not specific to this feature.

## faq

Questions a reader is likely to arrive with that the sections above do not answer directly, each with a short answer and a link to the section that gives the long one. Keep this to questions that have actually been asked or that a reviewer raised; it is not a place to restate the details.

## references

Links to the related pages in this documentation, to the source files that implement the feature, and to any external specification the feature follows. One line each, with a few words on what the reader finds there.
