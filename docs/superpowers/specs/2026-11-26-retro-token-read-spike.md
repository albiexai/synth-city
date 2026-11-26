# Retrospectives cost spike: per-member, per-window token reads (#1248)

Design: section 4 of `2026-11-15-campaign-retrospectives-design.md` (revision 4). Tracking issue: #944. Follow-up issues: #1249 (Claude Code reader) and #1250 (Codex reader).

## Verdict

- **Proven:** window token totals can be computed from the native session files of both harnesses. The samples below are **session-level measurements**.
- **Not proven:** cost for a member. Two checks are open for each harness: (1) the session was matched to a member by working directory only, not by the product's binding record; (2) the controller user's read access is inferred from the unit configuration and was not tested.
- **Fallback (issue #1248):** until both checks pass, **cost ships as unknown for each member**, with the reasons `binding_unverified` and `controller_read_unverified`. Do not show a member's cost from this report alone.
- **The reader needs six rules.** They are in "Rules for the readers" below.

I read one live session of each harness on 2026-11-26 at 08:00 UTC: B4's Claude Code session (working directory `build-4`) and the Codex session whose working directory is `build-1`. I read only numbers and field names. I did not copy or publish any prompt, answer or tool output.

## Observed and inferred

| Statement | Status |
|---|---|
| Claude rows carry `message.usage`, `message.model` and a time. | Observed in one live file. |
| Codex `token_count` rows carry cumulative `total_token_usage`. | Observed in one live file. |
| A window total can be computed for one session of each harness. | Observed. The numbers are below. |
| The files are mode 600 in a mode 700 folder (Claude) and group `deckproduct`, mode 660 (Codex). | Observed with `stat`. |
| The controller unit has `CAP_DAC_READ_SEARCH`. | Observed in `scripts/product-factory/albiex-synth-city-product.service`. |
| The running controller really has that capability and can read these files. | **Inferred.** Not tested as the controller user. |
| The product shows `native_progress` for member B4. | Observed in the member status. It is consistent with a controller read. It does not prove one. |

## Claude Code

- **Where:** `<home>/.claude/projects/<folder from the working directory>/<session id>.jsonl`. The session id is the file name and the `sessionId` field of each row.
- **What:** each `assistant` row has `message.usage` with `input_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens` and `output_tokens`. It also has `message.model`, `message.id` and a `timestamp`.
- **Repeated rows:** one message is written as several rows, one for each content block. In this file every message had identical usage in all its rows, and 1,915 rows held 974 messages. I measured the sum over all rows against the sum over messages: a reader that counts rows reports 1.96 times the input tokens, 2.17 times the cache creation tokens, 1.97 times the cache read tokens and **2.54 times the output tokens**. The ratio is not the same for each field, because a message with more content blocks has more rows. **Count each `message.id` once.**
- **Session total:** one `cost-state` row holds `totalCostUSD` and `modelUsage` for the whole session. It has no time, so it cannot give a window. Do not use it for windows.

Sample, one session (B4's, matched by working directory; one model, 958 messages, no row without usage, no sidechain row):

| Window (UTC, 2026-11-26) | Messages | Input | Cache creation | Cache read | Output |
|---|---:|---:|---:|---:|---:|
| 02:00 to 04:00 | 156 | 314 | 200,937 | 45,431,301 | 116,098 |
| 04:00 to 06:00 | 66 | 132 | 103,616 | 28,675,576 | 42,132 |
| 06:00 to 08:00 | 145 | 292 | 235,124 | 89,842,571 | 143,828 |

A message belongs to the window that holds its first row time.

## Codex

- **Where:** `<home>/.codex/sessions/<year>/<month>/<day>/rollout-<time>-<session id>.jsonl`. The first row is `session_meta` with the session `id` and the `cwd`.
- **What:** `event_msg` rows of type `token_count` have `info.total_token_usage` with `input_tokens`, `cached_input_tokens`, `cache_write_input_tokens`, `output_tokens`, `reasoning_output_tokens` and `total_tokens`. The values are cumulative for the session.
- **Rules seen in the data:** `total_tokens` equals input plus output. Cached input is part of input. Reasoning output is part of output. In this file `total_tokens` never decreased in 1,354 events. No event had a null `info`.
- **A window** is the difference of the last cumulative totals at its two edges.

Sample, one session (the one with working directory `build-1`, matched by working directory; model `gpt-6.2-sol`):

| Window (UTC, 2026-11-26) | Events | Input | Cached input | Output | Total |
|---|---:|---:|---:|---:|---:|
| 02:00 to 04:00 | 0 | 0 | 0 | 0 | 0 |
| 04:00 to 06:00 | 28 | 5,571,940 | 5,360,640 | 9,165 | 5,581,105 |
| 06:00 to 08:00 | 223 | 73,693,529 | 73,465,728 | 102,570 | 73,796,099 |

The first window has no event. The baseline is the last cumulative total at or before the window start (the session began before this window, so a baseline existed). The first and the last total of the window are the same, so the difference is zero. That is "no event seen", not proof that the member was idle or that no log row is missing: the report must show the event count next to every number.

## Attribution

- The native session identity is the session id plus the working directory. I read from the product code that its activity observers use this pair. **I did not read the product's binding record of any member.** Each sample was matched to a member by its working directory only: B4 by the repository path in its own member status, and the Codex member by the name of its working directory. So the match to the current binding is **inferred, not shown**. #1249 and #1250 must test it against the product's binding record.
- A member has more than one native session over time. In the folders I read: 3 Codex files for the `review` directory, 2 Claude files each for `build-1` and `build-2`. A window read must cover every session of the member in the window. A session that is not bound to a member is **unattributed usage**: show it, never drop it.

## Rules for the readers (#1249 and #1250)

1. Claude Code: count each `message.id` once. Take the message time from its first row.
2. Codex: window usage is the difference of cumulative totals. If a total decreases, treat it as a reset: add the new total as new usage. No reset occurred in my sample, so this rule is **proposed, not observed**.
3. Show the sample count (messages or events) next to every number.
4. A file the controller cannot read, an unbound session, or an unknown format gives **unknown with a reason**, never zero. Suggested reasons: `native_log_unreadable`, `no_native_session`, `format_unknown`, `binding_unverified`, `controller_read_unverified`.
5. Show a member's cost only after the session is matched to the member's binding record and the controller read is verified for that harness. Until then the member's cost is unknown.
6. Read bounded: use the descriptor, check the owner, refuse a file others can write, and cap the bytes. The existing activity readers in `claude_activity_service.py` and `agent_activity_service.py` already do this.

## Limits and unknowns

- One live member for each harness, one session each, one day. This is a spike, not a sample of the campaign.
- Not tested: a read as the controller user; a Claude session that used subagents (sidechain rows, separate files); a Codex session with a model change inside the session; a Codex counter reset; a session resumed under a new id.
- The Claude file grows while it is read. The numbers are as of 2026-11-26 08:00 UTC. Windows that have closed do not change.
- Prices are out of scope here (#1249).

## How to repeat the check

For Claude Code, read each line of the transcript as JSON, keep rows with `type == "assistant"`, keep the first row of each `message.id`, and sum the four usage fields by window. For Codex, read the rollout, keep `event_msg` rows with `payload.type == "token_count"`, and subtract the `total_token_usage` that was last at or before the window start from the one at the window end.
