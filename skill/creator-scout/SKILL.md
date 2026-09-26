---
name: creator-scout
description: Find small, niche gaming and tech creators on TikTok and YouTube (including Shorts) in any country and language, judge their fit for a brand, and produce an outreach-ready spreadsheet. Use this whenever someone asks to find influencers, creators, streamers, TikTokers or YouTubers for a market or country, to expand or refresh a creator list, to check which creators a market has for a gaming/PC brand, or to prepare creator outreach — even if they don't name the tool.
---

# Creator Scout

Creator Scout does the job of a person scrolling TikTok and YouTube country by
country: it searches the platforms in the local language, keeps only accounts
that belong to the market and fit the size band, has fit judged, and expands
through the following lists of good creators. You (Claude) act as the judge;
the `scout` CLI does everything deterministic (API calls, filters, metrics,
export, checks).

## Before you start

1. Check the setup: `scout credits` should print a credit balance, and the
   environment must have `SC_KEY` (ScrapeCreators) and `YOUTUBE_API_KEY`.
   Without `SC_KEY` only saved runs can be explored.
2. Turn the request into: market (`scout markets` lists them; add a
   `scout/markets/<cc>.yaml` for a new one), platforms, size preset
   (`default`: TikTok 4k-500k, YouTube 50k-250k; `hidden-gems`: TikTok 1k+,
   YouTube 5k+ — use it for small markets), target count and credit budget.
3. Tell the user the expected credit cost before any live run. A useful run is
   150-300 credits; roughly 3-9 accepted creators per 100 credits in small
   markets.

## Workflow

```bash
scout run --market ee --preset hidden-gems --budget 200 --judge claude
```

With `--judge claude` the run pauses whenever it needs a judgment:

1. **Pre-judge** (status `awaiting_prejudge`): `scout candidates --stage prejudge`
   prints accounts with only free data (bio, a few captions, how they were
   found). Answer `yes`, `unsure` or `no` per id. Only a confident `no` is
   skipped, so when in doubt say `unsure` — a missed creator costs more than one
   extra lookup. Write `{"results": [{"id": ..., "verdict": ..., "reason": ...}]}`
   to a file and run `scout decide --stage prejudge --file prejudge.json`.
2. `scout resume` continues: it enriches the yes/unsure accounts (profile,
   recent videos, YouTube uploads) and pauses again with `awaiting_judgment`.
3. **Judge**: `scout candidates --stage judge` prints each candidate's evidence.
   Judge with `references/judging-rubric.md` and write one result per id,
   including `platform`, `decision`, scores, `niche_category`, `games`,
   `market_resolution`, `content_language`, `reasons` and a verbatim
   `evidence_quote`. Import with `scout decide --stage judge --file judge.json`.
4. `scout resume` again. Accepted creators become snowball seeds, and the run
   alternates their following lists with new search pages. Repeat steps 1-4
   until the run reports `target_met`, `budget_exhausted` or `done`.
5. `scout check` must pass before you report anything. Then `scout export`
   and `scout yield` for the numbers.

Report to the user: accepted creators, credits spent, where they came from
(`scout yield`), anything flagged (competitor sponsorship, brand safety,
inactive, above typical size) and the path to the XLSX.

## Why the rules are what they are

- **Evidence must be verbatim.** The code checks that the quote appears in the
  bio, a caption or a video title and downgrades an accept to maybe if not.
  This keeps judgments tied to what the creator actually posts.
- **Business and organisation accounts are rejected or held at maybe.** Shops
  and brands are not partners; the tool keeps them as data but not as leads.
- **Competitor sponsorship is a flag, not a reject.** A creator who did a paid
  post for a competitor has proven they take deals; the sheet shows the risk.
- **Market resolution matters.** TikTok's region is the registration country;
  YouTube's country is self-declared. For accounts marked `unsure`, decide from
  language and local references; say `unclear` rather than guess.
- **Nothing is discarded.** Every account keeps its status and reason, and
  accounts that belong to another market are queued for that market.

## Other commands

| Command | Use |
|---|---|
| `scout keywords --market ee [--refresh]` | show or regenerate the search plan |
| `scout recall --market ee` | if a client partner list is configured: which partners the tool found and why others were missed |
| `scout partner-seeds --market ee` | use existing partners as snowball seeds (`--partner-seeds` on run/resume) |
| `scout pitches --run N` | optional outreach drafts (a person reviews and sends) |
| `scout reset --market ee --yes` | forget a market's screening state (keeps the API cache) |

Brand context for judging lives in `scout/brand.yaml` and is summarised in
`references/brand-brief.md`. Outreach tone is in `references/outreach-style.md`.
