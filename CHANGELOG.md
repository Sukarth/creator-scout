# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- YouTube as a core platform: ScrapeCreators search with a region (1 credit
  per page) for discovery; the official YouTube Data API (`YOUTUBE_API_KEY`,
  free quota) for country, subscribers, description and recent uploads, with
  long videos (`UULF`) and Shorts (`UUSH`) measured separately. Accepted
  channels get one ScrapeCreators channel lookup for email and links.
- Cross-platform identities: TikTok accounts that expose a YouTube channel id
  and channels that link a TikTok handle are linked and exported as one row.
  A linked TikTok account of an accepted channel becomes a snowball seed.
- Size bands per platform with presets: `default` (TikTok 4k-500k, YouTube
  50k-250k) and `hidden-gems` (TikTok 1k+, YouTube 5k+).
- View metrics as clients quote them: average views over the last 30 days (90
  when fewer than 3 videos), with the window and video count stated, a
  20th-80th percentile range ("10K-30K") and a recent-versus-older trend.
- Judge output adds a niche category, the games covered and a
  young-gamer-appeal score; fit is judged as "would a young PC-gaming audience
  watch this", with gaming and tech as the core.
- Existing client partners (private list, `SCOUT_PARTNERS_FILE`): marked and
  listed separately in exports; `scout recall` reports which partners a run
  found by itself and at which stage the others were lost; `scout
  partner-seeds` adds them as snowball seeds afterwards.
- Export: one row per creator with the required columns first (country,
  followers/subscribers, average views with window, niche and games, contact),
  then risks (competitor sponsorship, brand safety, inactivity) and trend; a
  "Prenew format" sheet mirrors the client's collaboration sheet.
- `scout check` requires the client's columns to be filled for shortlisted rows.

- More platform search: TikTok top search, keyword search sorted by likes,
  TikTok user search; YouTube channel search, Shorts search, Shorts hashtags
  and the official Shorts-length search (`search.list`, free quota). Shorts
  results carry no channel, so their channels are resolved with the free API.
- LLM query generation adds 40-60 TikTok queries (game names in local
  phrasing, creator-style and tech-review phrases), YouTube queries and
  Shorts hashtags per market.
- YouTube channels are labelled Shorts-first, long-form or mixed from their
  upload counts; Shorts and long-video views are reported separately.

### Changed
- Harvest budget follows yield: each source group gets exploration pages,
  then the next page goes to the group with the most accepted creators,
  pre-judge "yes" and new in-market accounts per credit. Sources paginate
  deeper while they keep yielding.
- Size limits are soft: creators up to 1.5x the upper limit are kept and
  flagged "above typical range"; recall counts partners found outside the
  band as found.
- No credits are spent on contact enrichment; contacts come only from bios,
  channel descriptions and bio-link pages.
- Snowballing starts as soon as two creators are accepted and then alternates
  two following-list pages with one harvest page; seeds are expanded while
  they keep yielding (up to 30 pages).
- Pitches are opt-in (`--pitches`); they are not part of a run by default.
- Free LLM provider chain (`scout/llm.py`): Groq `gpt-oss-120b`, `qwen3.8-27b`,
  `gpt-oss-20b` and OpenCode Zen `space-bunny-free`, OpenAI-compatible JSON
  mode, per-model sliding token window under the 8k tokens-per-minute free
  limit, cooldown on 429, one repair retry on invalid JSON, response cache.
- Pre-judge: every in-market, in-band candidate is triaged (yes / unsure / no)
  in batches of up to 30 from free data only (nickname, bio, harvested captions
  and hashtags, how it was found). Only a confident "no" skips paid enrichment;
  missing answers count as "unsure". Gaming terms only raise priority.
- Fit judge with a strict schema (decision, fit score, market resolution,
  content language, niche, trust-content and gaming relevance scores,
  sponsors, competitor conflict, evidence quote). Code-side rules: competitor
  matches force the conflict flag, business accounts are rejected, accepts
  whose evidence quote is not found in the bio or captions become "maybe",
  unsure-market accounts the judge places elsewhere go to the other-markets pool.
- Pitch drafts (subject, body under 120 words, DM under 300 characters) in the
  creator's content language, with an opt-out line.
- LLM-generated local hashtag and keyword ideas per market (`scout keywords`).
- Skill-mode handoff: `--judge claude` pauses the run; `scout candidates`,
  `scout decide` and `scout resume` exchange pre-judge, judge and pitch
  results as JSON files. `scout pitches` drafts pitches for a finished run.
- `scout reset --market` forgets a market's screening state while keeping
  creators and the API cache.
- Brand brief in `scout/brand.yaml` (description, trust points, what a good
  partner is, competitors) used by all prompts.
- Export: pitch columns, pre-judge verdict, content language, gaming relevance;
  per-source yield (seen, in market, enriched, accepted) and accepted creators
  by first source in the run log.
- `scout check`: pitch language must match the creator's content language.

- Project skeleton: `scout` package, `pyproject.toml`, `.env.example`, MIT licence.
- SQLite store for creators, per-market screenings, edges, videos, metrics,
  snowball seeds, runs and the API response cache. Screenings are per market,
  so an account filtered for one market remains available to others.
- ScrapeCreators client: cache-first, per-run credit budget, records
  `credits_charged` and `credits_remaining` from every live response. Client
  errors (e.g. unknown handle) are cached so a miss is never paid twice.
- Market configs for Estonia, Finland and Germany: languages, market signals,
  seed hashtags and keywords, retailer and competitor seed accounts.
- TikTok response normalisation for hashtag, keyword, following, user search,
  profile, region and recent-videos endpoints.
- Hard filters: market bucket (sure / unsure / other) from registration region,
  region lookup, app and caption language, video region and bio/caption
  signals; follower band; private; activity. Every filtered account keeps its
  reason; out-of-market accounts registered in another supported market are
  queued for that market.
- Enrichment: profile, recent videos, median views, engagement rate, posting
  cadence, ad markers (platform paid-partnership flag and caption markers),
  sponsor handles, competitor matching, price estimate and deal suggestion.
  Bio-link pages are fetched over plain HTTP for extra emails and social links.
- Snowball through following lists of qualified creators, business accounts
  and retailer seeds, prioritised by new in-market accounts per credit.
  Transient failures skip a seed or candidate for the current run only.
- XLSX export (Shortlist, Maybe, Other markets pool, Seeds and sources, All
  screened, Run log) with an outreach status dropdown, plus one CSV per sheet.
- `scout check` quality gates: band, market resolution, no duplicates from
  earlier runs, evidence quotes, contact path, funnel totals.
- CLI: `markets`, `run`, `export`, `check`, `credits`, `api`, `cache stats|clear`.

### Changed
- Harvest plan is gaming-first: local-language gaming hashtags (curated, then
  generated), then global game hashtags fetched through a proxy in the market
  and filtered by region, then keyword searches. General country tags such as
  `#eestitiktok` are opt-in (`--general-tags`).
- Snowball seeds are accepted creators only. Retailer and shop accounts are
  opt-in (`--retailer-seeds`): they mostly follow mainstream influencers.
- Estonian market config adds Russian gaming hashtags, keywords, city names
  and ad markers.
- Enrichment runs in chunks between pre-judge and judge rounds, so the target
  check stays current and the budget goes to the most promising candidates.
- Test fixtures are anonymised: every email is replaced by a deterministic
  placeholder that keeps the top-level domain.

### Fixed
- Harvest source groups (local hashtags, YouTube searches, keyword searches,
  global tags) take turns page by page; before, YouTube could get no budget
  when TikTok hashtags and snowballing came first.
- The judge's decision is kept consistent with its own score thresholds
  (relevance >= 3 or young-gamer appeal >= 4 with verified evidence accepts).
- Re-judging drops pitches written for the previous decision.
- Accounts without a known region that are seen while scouting one market are
  queued for any other supported market whose language or signals they match.
- `scout refresh-metrics` recomputes view windows from stored videos.
- Market languages that are widely spoken elsewhere (`shared_languages`,
  Russian for Estonia, Swedish for Finland) no longer make an account a
  market candidate on their own; a region match or another signal is needed.
  Without this, Russian-speaking accounts from any country entered the
  Estonian candidate pool.
- Unsure-market accounts get the 1-credit region lookup before the profile.
- Enrichment order puts confirmed-market accounts first; hashtag authors with
  unknown follower counts are ranked by the harvested video's views.
- Judge rubric: gaming and streaming creators qualify; trust content is a
  ranking score, not a requirement. The relevance scale has explicit anchors.
- Edges are stored per run, so accounts re-found in a later run are
  attributed to that run's sources.
- `scout resume --rescreen --rejudge` re-applies filters and the judge to a
  stored run without new API calls.
- Market words matched as prefixes (Estonian "tere" inside "Terezinha");
  they now match whole words only.
- Social links scraped from bio-link pages could pick up asset paths such as
  `instagram.com/rsrc.php`. Reserved paths and file names are ignored, and
  login-walled bio links (Instagram, YouTube, Twitch, X) are read from the URL
  without fetching the page.
- Hashtag and keyword harvest stops after a short page instead of paying for
  the empty page that usually follows.

### Notes
- Keyword search results carry no author region, but each video has its own
  `region`; it is used as a market hint so out-of-market authors cost nothing.
- Estonian keyword search returns mostly unrelated results; hashtags are the
  primary harvest source for small-language markets.

