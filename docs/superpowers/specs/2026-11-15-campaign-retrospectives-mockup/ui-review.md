# Review: campaign retrospectives mockups (design revision 4)

Reviewed: `15-retro-v44-mockups-acting.html` against the revision 4 design and the frontend in `wt-943`.
Revised mockup: `retrospectives-mockup.html` (seven tabs; rendered and checked at 1440 px and 390 px, no horizontal overflow).
**[done]** = reflected in the revised mockup. **[rec]** = recommendation only.

## Findings, most important first

**1. Live page: the answer to "what should I do" is below five sections.** [done]
The Director scrolls past Headline, Time, Flow, CI and Friction (about three screens) to reach "Top bottlenecks".
Change: "Where the time went" and "Top bottlenecks, and what to do" sit side by side directly under the tiles, so the first screen at 1440 px answers how it is going, what slows it and what to do. Flow, CI, Friction and Size follow as evidence. An "On this page" row jumps to each section. At narrow widths the band stacks: time, then bottlenecks.

**2. The bottleneck ranking contradicts the design's own rule.** [done]
The design ranks by time, with a 10% minimum share of waiting-and-process time. In the mockup, "e2e queues" (1.2 h, 8%) and "Waiting for the Supervisor" (1.6 h, 10%) outrank Required checks (5.4 h) and Review (3.3 h), and the Supervisor card says "no rule matched" while the settings dialog for it says "rule fired".
Change: illustrative data now follows the rule (Leader 27%, e2e 20%, Supervisor 15%; two more named as "also above the thresholds"). Each card shows time, share and items. "Investigate" (no rule) is drawn separately in its three steps: not asked, asked, diagnosis received.

**3. Bars in "Where the time went" are scaled per group.** [done]
"Leader decisions 4.2 h" and "Required checks 5.4 h" are both drawn at 62%; Building (11.2 h) at 92%. Bar length means nothing across groups, on the core measure.
Change: one scale for every stage, stated in the caption. A real table (stage, who had to act, bar, time, share, count) replaces the fixed-width grid. The split bar has a text legend with share and hours instead of labels inside segments, which truncate when narrow.

**4. Six buttons in the header, with "Finish campaign" as the only primary.** [done]
Four Ask buttons plus an irreversible primary action compete with the page title at 65% progress.
Change: Ask moved to where the answer appears: each role's card in Agents' views has its own Ask button and request state, and each bottleneck has its contextual Ask. "Finish campaign…" is an outline button that opens a confirmation; it becomes primary only in the "ready to finish" banner.

**5. The frozen page is a different page.** [done]
It drops Flow, CI, Friction, Size and the stage breakdown, changes the tiles, and removes section numbers. The design says the same layout, frozen.
Change: same header, tiles, sections and order. Bottlenecks become Lessons in the same position. A "Frozen" note states read-only. The lessons table separates "What happened" from "Before → after (median, n)". The cross-campaign aside in the Hand merges tile is removed (comparison is deferred).

**6. Dialogs: four panels mixing seven things, no consistent anatomy or size.** [done]
Standing decision shares a panel with the restart checklist; the agent proposal shares one with the defect. None states a size. The first bottleneck has no action button at all.
Change: one dialog per kind of fix, each with Evidence, Lever, Your decision in that order, footers with Cancel left of one primary, and `MODAL_SIZES.SM` or `MD` stated. Added the two missing dialogs: Ask an agent (role question, what is sent, optional extra question) and Finish (what happens, open issues, time limit). The setting dialog now holds evidence and what-if and ends in "Open the setting ›", which matches the decision that Apply is a link with nothing prefilled. Buttons on the live page open these as real dialogs.

**7. Unknown, partial and stale are carried by colour or a tooltip.** [done]
"unknown" is amber text; one reason sits in a `title` attribute (no keyboard or touch access); cost coverage is small grey text; "new since your last visit" is red, which reads as an error.
Change: `unknown` is a dashed chip with the reason as visible text (one note per table). Partial cost carries a "partial" badge and names the missing member. Stale views carry an "Out of date" badge. Small samples (n below the minimum) are labelled. "New" is a neutral outline badge.

**8. Too many meaning-colours.** [done]
Four badge colours for the kind of fix, four matching left stripes, three group colours, plus amber and red states. The red "Synth City defect" badge reads as a failure.
Change: kinds of fix are neutral outline badges; the word carries the meaning. Colour is kept for three things: the green primary, the product's amber "needs you", and the three time groups (palette checked for colour-blind separation, always with a text label). Agent judgement is marked by a "Judgement" badge and a distinct card, not a dashed border alone.

**9. Overview: the mockup replaces the existing Progress card.** [done]
The real `CampaignProgress` card has Done, Under way, Still to do, decisions ahead and footer links; the section is titled "Progress", not "Campaigns". "Needs you" has its own established style.
Change: the existing card is kept and gains one "Campaign health" block (item time, cost with coverage, top bottleneck in the last 4 hours) with a "Live retrospective ›" link in the block and in the existing footer link row. The finished campaign is one text line, not a card at 75% opacity (low contrast).

**10. 390 px is unusable.** [done]
A fixed 190 px sidebar, a `230px 1fr 90px 120px` grid, eight-column tables and SVG text that scales down to about 5 px.
Change: icon-rail sidebar as in the product; tiles two across; list, per-repository, before-and-after and lessons tables become labelled rows; CI tables scroll inside a focusable region; the flow chart is laid out in CSS so labels keep their size. Tab 7 shows the live page in a 390 px frame.

**11. Missing pieces a builder needs.** [done]
Added: the per-repository table and the "team-wide" tag when one repository is chosen (design §8); item time by size band (§3); empty list; filters matching nothing; loading; stale read; a pre-4.4 campaign with one reason for the whole page; cost unknown; too few items for a bottleneck; ready-to-finish banner; the **Finishing** state between Finish and the freeze; no Supervisor on the team.

**12. Fit with the product.** [done]
- Sidebar order and entries were wrong ("Live sessions" missing). Now as in `Sidebar.tsx`, with Audit renamed Insights.
- Filters were dropdown buttons; the product uses labelled native selects (`FactoryFilters`). The tabs follow shadcn `TabsList`.
- Page titles and section headings follow the product scale (`text-2xl`, `text-lg`).
- No clickable cards are needed: campaign names and row headers are links, so `CLICKABLE_CARD` does not apply. If the builder makes list rows cards on mobile, it must be used.
- Every section links to the Audit log filtered to its events ("events ›"), as the design requires; the original had one such link.

**13. Smaller points.** [done]
Units in column headers; right-aligned tabular numbers; change markers as numbered pins with a text table; one measure per before-and-after row; the flaky count as a CI table column.

## Not done in the mockup

- **Dark theme.** [rec] The product has one. Tokens are variables, but the three chart colours need their own dark steps.
- **Chart tooltips.** [rec] Bars carry native titles only; the build uses Recharts `ChartTooltip` plus the table below the chart.
- **Review, by verifier.** [rec] "Review" should split per verifier in the stage table, since a bottleneck is ranked per responsible actor.
