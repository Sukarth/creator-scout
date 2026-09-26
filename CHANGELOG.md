# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
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
